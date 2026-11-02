#!/usr/bin/env python3
"""One agent-deck Claude session per Asana task.

Picks tasks the way a listing does — the same `/search` filters the
`task-lister` agent uses — and gives each one its own agent-deck session,
with that task's full context (notes, subtasks, comments) in its system
prompt and a start prompt that sets it working (`--idle` opts out). `agent-deck -g tasks` is the view;
`agent-deck session attach <id>` opens one to work by hand.

`scripts/link-skills.sh` symlinks this onto PATH as `task-sessions`.

    task-sessions --due-before 2026-09-18            # dry run: what it would do
    task-sessions --due-before 2026-09-18 --spawn    # launch them
    task-sessions 1218118170820306 --spawn           # explicit GIDs
    task-sessions 1218118170820306 --spawn --refresh # also resend fresh context
    task-sessions --prune --spawn                    # archive sessions of completed tasks
    task-sessions --print-context 1218118170820306   # the block a session would boot with

agent-deck's registry is the only state. Each session carries the hint
`asana_gid=<gid>`; a run lists the registry, reads those hints, and leaves a
task with a live session alone, reopens a stopped one (same conversation), and
launches the rest. A launch sends START_PROMPT (or `--prompt`) as the first
message, so the session starts working at once; `--idle` sends nothing and
the session waits at an empty prompt at zero cost. Reopened sessions are never
prompted. Design:
docs/superpowers/specs/2026-11-02-agent-deck-task-sessions-design.md.

Group: --group, else $TASK_SESSIONS_GROUP, else `tasks`.
Parent: --parent none|self|<session>, else $TASK_SESSIONS_PARENT, else
`conductor-tasks` (the tasks conductor) — or none when that session doesn't exist.

Stdlib only — this runs from PATH under whatever `python3` is first, not
under the repo's venv.
"""

import argparse
import datetime
import functools
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_ref

REPO = Path(__file__).resolve().parent.parent
CONTEXT_DIR = Path.home() / ".claude" / "task-context"
API_BASE = "https://tasks-api.drolet.cloud"
NAME_MAX = 60
SHORT_ID = 8
DEFAULT_LIMIT = 10
DEFAULT_GROUP = "tasks"
DEFAULT_PARENT = "conductor-tasks"
START_PROMPT = """\
Start working this task now. The task context in your system prompt is a snapshot, \
so first re-fetch the task (fetching-task skill) for its current state.

Then:
1. Work out what the task actually requires and by when.
2. Research what it needs — related email, related tasks, documents, the web — \
and do any preparatory work that is safe and reversible (drafts, notes, lookups).
3. Stop and report: what the task requires, what you found and did, the next \
concrete step, and anything you need from Ben.

Do not send email or messages, make payments, submit forms, complete or edit the \
Asana task, or take any other outward-facing or irreversible action without \
asking first. If the task looks like a duplicate of another open task, say so \
and stop."""
GID_HINT = "asana_gid"
LIVE = {"running", "waiting", "idle", "starting"}
REOPENABLE = {"stopped", "error"}

ref_for = task_ref.ref


class DeckUnavailable(Exception):
    """agent-deck could not be asked, so nothing it says can be trusted."""


# --- tasks-api -----------------------------------------------------------


@functools.cache
def token() -> str:
    """A Google ID token from the caller's gcloud login — tasks-api is behind Cloud Run IAM."""
    try:
        out = subprocess.run(
            ["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, check=True
        )
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"task-sessions: gcloud auth print-identity-token failed: {e}")
    return out.stdout.strip()


def _call(path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def search(**filters) -> list[dict]:
    return _call("/search", {k: v for k, v in filters.items() if v is not None}).get("results", [])


def fetch(gid: str) -> dict:
    return _call(f"/tasks/{gid}")


# --- the context a session boots with ------------------------------------


def context_block(task: dict, ref: str) -> str:
    """The markdown a session carries in its system prompt."""
    gid = task["task_gid"]
    lines = [
        "# Active task",
        "",
        f"You were opened to work one Asana task. It is `{ref}` · GID `{gid}`.",
        "",
        f"**{task['name']}**",
    ]

    facts = [
        ("GID", gid),
        ("Ref", ref),
        ("Link", task.get("permalink_url")),
        ("Due", task.get("due_on") or task.get("due_at")),
        ("Project", task.get("project")),
        ("Section", task.get("section")),
        ("Tags", ", ".join(task.get("tags") or []) or None),
        ("Assignee", task.get("assignee")),
    ]
    parent = task.get("parent")
    if parent:
        facts.append(("Parent", f"{parent['name']} (GID {parent['gid']})"))
    lines += [""] + [f"- {label}: {value}" for label, value in facts if value]

    notes = (task.get("notes") or "").strip()
    if notes:
        lines += ["", "## Description", "", notes]

    subtasks = task.get("subtasks") or []
    if subtasks:
        refs = task_ref.assign([s["task_gid"] for s in subtasks])
        lines += ["", "## Subtasks", ""]
        for subtask in subtasks:
            box = "x" if subtask.get("completed") else " "
            due = f" · due {subtask['due_on']}" if subtask.get("due_on") else ""
            lines.append(
                f"- [{box}] `{refs[subtask['task_gid']]}` {subtask['name']}"
                f" (GID {subtask['task_gid']}){due}"
            )

    comments = task.get("comments") or []
    if comments:
        lines += ["", "## Comments", ""]
        for comment in comments:
            when = (comment.get("created_at") or "")[:10]
            who = comment.get("created_by") or "unknown"
            text = " ".join((comment.get("text") or "").split())
            lines.append(f"- {who} ({when}): {text}")

    lines += [
        "",
        "Work only this task. Re-fetch it with the fetching-task skill if you "
        "need current state — the context above is a snapshot from when this "
        "session was spawned.",
    ]
    return "\n".join(lines)


def deck_section(title: str, group: str | None, parented: bool) -> str:
    """Stands in for agent-deck's identity block, which our context file displaces.

    Claude keeps only the last --append-system-prompt-file it is given, so the
    launch passes --no-identity and carries the parts that matter here (spec D4).
    """
    where = f" in group `{group}`" if group else ""
    lines = [
        "",
        "",
        "## agent-deck",
        "",
        f"You run inside agent-deck as session `{title}`{where}. "
        "`agent-deck session current --json` returns your live record.",
    ]
    if parented:
        lines += [
            "",
            "A parent session supervises you. When the work you were given is fully "
            "done, end your final message with exactly one line:",
            "`===AGENTDECK_DONE=== status=<ok|fail> summary=<one line>`",
            "To ask the parent something, start a line with `NEED:`.",
        ]
    return "\n".join(lines)


def session_name(ref: str, name: str) -> str:
    full = f"{ref} {name}"
    return full if len(full) <= NAME_MAX else full[: NAME_MAX - 1] + "…"


def write_context(gid: str, text: str, directory: Path | None = None) -> Path:
    directory = directory or CONTEXT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{gid}.md"
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(text)
    return path


# --- configuration -------------------------------------------------------


@dataclass
class Placement:
    group: str | None  # None = inherit the parent's group
    parent: str | None  # resolved session id/title; None = no parent
    parent_is_default: bool = False

    def flags(self) -> list[str]:
        flags = ["--parent", self.parent] if self.parent else ["--no-parent"]
        flags += ["-g", self.group] if self.group else ["--inherit-group"]
        return flags


def resolve_placement(args, env=os.environ) -> Placement:
    explicit = args.parent or env.get("TASK_SESSIONS_PARENT")
    parent = explicit or DEFAULT_PARENT
    if parent == "none":
        parent = None
    elif parent == "self":
        parent = env.get("AGENTDECK_INSTANCE_ID")
        if not parent:
            raise SystemExit(
                "task-sessions: --parent self needs to run inside an agent-deck session"
            )
    if args.inherit_group:
        if not parent:
            raise SystemExit("task-sessions: --inherit-group needs a parent")
        if args.group:
            raise SystemExit("task-sessions: --inherit-group and --group conflict")
        return Placement(group=None, parent=parent, parent_is_default=not explicit)
    group = args.group or env.get("TASK_SESSIONS_GROUP") or DEFAULT_GROUP
    return Placement(group=group, parent=parent, parent_is_default=not explicit)


def check_parent(placement: Placement, deck_rows: list[dict]) -> str | None:
    """Drop a default parent that isn't in the registry; refuse an explicit one.

    Returns a warning when the default conductor is missing — the sessions
    still launch, just unsupervised.
    """
    if not placement.parent:
        return None
    known = any(
        placement.parent in (row.get("id"), row.get("title"))
        or (len(placement.parent) >= 6 and (row.get("id") or "").startswith(placement.parent))
        for row in deck_rows
    )
    if known:
        return None
    if placement.parent_is_default:
        missing = placement.parent
        placement.parent = None
        if placement.group is None:
            raise SystemExit("task-sessions: --inherit-group needs a parent that exists")
        return f"parent {missing} not found; launching without a parent"
    raise SystemExit(f"task-sessions: parent {placement.parent} not found in agent-deck")


# --- agent-deck ----------------------------------------------------------


def _run(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=REPO, input=stdin, capture_output=True, text=True, check=False)


def _json(result: subprocess.CompletedProcess) -> dict | list | None:
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None


def _error(result: subprocess.CompletedProcess) -> str:
    body = _json(result)
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"]).splitlines()[0]
    text = (result.stderr or result.stdout or "").strip()
    return text.splitlines()[0] if text else f"exit {result.returncode}"


def listing(runner=_run) -> list[dict]:
    """Every session in the profile. Raises when the answer can't be trusted (defect 1)."""
    try:
        result = runner(["agent-deck", "list", "--json"])
    except OSError as e:
        raise DeckUnavailable(str(e))
    rows = _json(result)
    if result.returncode != 0 or not isinstance(rows, list):
        raise DeckUnavailable(_error(result))
    return rows


def hints(session_id: str, runner=_run) -> dict:
    result = runner(["agent-deck", "session", "annotate", session_id, "--json"])
    body = _json(result)
    if result.returncode != 0 or not isinstance(body, dict):
        raise DeckUnavailable(f"annotate {session_id}: {_error(result)}")
    return body.get("hints") or {}


def launch_argv(
    *,
    title: str,
    gid: str,
    permalink: str | None,
    context_file: Path,
    placement: Placement,
    model: str | None = None,
    idle_timeout: str | None = None,
    prompt: bool = False,
) -> list[str]:
    argv = ["agent-deck", "launch", "--json", "--trust-folder", "-c", "claude", "--no-identity"]
    argv += placement.flags()
    argv += ["-t", title, "--hint", f"{GID_HINT}={gid}"]
    if permalink:
        argv += ["--hint", f"permalink={permalink}"]
    if model:
        argv += ["--model", model]
    if idle_timeout:
        argv += ["--idle-timeout", idle_timeout]
    argv += [
        "--extra-arg=--append-system-prompt-file",
        f"--extra-arg={context_file}",
    ]
    if prompt:
        argv += ["--message-file", "-"]
        if not placement.parent:
            argv.append("--no-assert-done")
    argv.append(str(REPO))
    return argv


def ensure_group(group: str, runner=_run) -> str | None:
    """Create the group if missing; return a warning when its cap would serialize sessions."""
    result = runner(["agent-deck", "group", "show", group, "--json"])
    body = _json(result)
    if result.returncode != 0 or not isinstance(body, dict) or not body.get("success", True):
        created = runner(
            [
                "agent-deck",
                "group",
                "create",
                group,
                "--default-path",
                str(REPO),
                "--max-concurrent",
                "0",
            ]
        )
        if created.returncode != 0:
            return f"could not create group {group}: {_error(created)}"
        # `group create --max-concurrent 0` is ignored (v1.16.36 stores the serial
        # default of 1); `group update` honours 0. Only a group we just created is
        # touched — an existing group's cap is the user's choice.
        updated = runner(["agent-deck", "group", "update", group, "--max-concurrent", "0"])
        return (
            None if updated.returncode == 0 else f"could not uncap group {group}: {_error(updated)}"
        )
    cap = body.get("max_concurrent", 0)
    if cap not in (0, None):
        return f"group {group} has max_concurrent={cap}; sessions given a prompt will queue"
    return None


# --- planning ------------------------------------------------------------


@dataclass
class Row:
    ref: str
    gid: str
    task: dict
    sessions: list[dict] = field(default_factory=list)
    adopt: bool = False
    outcome: str = ""
    deck_id: str | None = None
    status: str | None = None
    group: str | None = None

    @property
    def title(self) -> str:
        return session_name(self.ref, self.task["name"])


def in_tree(session_group: str | None, group: str | None) -> bool:
    """True when a session sits in the group or one of its sublists (`tasks/waiting-on-others`).

    The tasks conductor files sessions into sublists by what they are waiting on;
    they are still task sessions.
    """
    if not group or not session_group:
        return False
    return session_group == group or session_group.startswith(f"{group}/")


def find_sessions(rows: list[Row], deck_rows: list[dict], group: str | None, runner=_run) -> None:
    """Attach each row's existing sessions: the hint is identity, the title prefix finds hand-made ones."""
    by_gid = {row.gid: row for row in rows}
    prefixes = {f"{row.ref} ": row for row in rows}
    for session in deck_rows:
        title = session.get("title") or ""
        prefix_row = next((r for p, r in prefixes.items() if title.startswith(p)), None)
        in_group = in_tree(session.get("group"), group)
        if not in_group and prefix_row is None:
            continue
        gid = hints(session["id"], runner).get(GID_HINT)
        if gid in by_gid:
            by_gid[gid].sessions.append(session)
        elif gid is None and prefix_row is not None:
            prefix_row.sessions.append(session)
            prefix_row.adopt = True


def _short(session_id: str) -> str:
    return session_id[:SHORT_ID]


def act(row: Row, args, placement: Placement, runner=_run) -> None:
    """Decide and (under --spawn) carry out one task's outcome. Never raises for one task."""
    spawn = args.spawn
    if len(row.sessions) > 1:
        row.outcome = "duplicate " + ",".join(_short(s["id"]) for s in row.sessions)
        return

    context = context_block(row.task, row.ref) + deck_section(
        row.title, placement.group, bool(placement.parent)
    )

    if row.sessions:
        session = row.sessions[0]
        sid, status = session["id"], session.get("status")
        row.deck_id, row.status, row.group = sid, status, session.get("group")
        if spawn and row.adopt:
            runner(
                ["agent-deck", "session", "annotate", sid, "--set-hint", f"{GID_HINT}={row.gid}"]
            )
        if session.get("archived"):
            if not spawn:
                row.outcome = f"would reopen {_short(sid)}"
                return
            runner(["agent-deck", "session", "unarchive", sid])
            status = "stopped"
        if status in REOPENABLE:
            if not spawn:
                row.outcome = f"would reopen {_short(sid)}"
                return
            write_context(row.gid, context)
            result = runner(["agent-deck", "session", "start", sid])
            row.outcome = (
                f"reopened {_short(sid)}" if result.returncode == 0 else f"failed: {_error(result)}"
            )
            if args.refresh and result.returncode == 0:
                _refresh(row, sid, context, runner)
            return
        if args.refresh and status in LIVE - {"running"}:
            if not spawn:
                row.outcome = f"would refresh {_short(sid)}"
                return
            _refresh(row, sid, context, runner)
            return
        row.outcome = f"already open {_short(sid)} ({status})" + (
            " · adopted" if row.adopt and spawn else ""
        )
        return

    if not spawn:
        row.outcome = "would launch"
        return
    context_file = write_context(row.gid, context)
    argv = launch_argv(
        title=row.title,
        gid=row.gid,
        permalink=row.task.get("permalink_url"),
        context_file=context_file,
        placement=placement,
        model=args.model,
        idle_timeout=args.idle_timeout,
        prompt=bool(args.prompt),
    )
    result = runner(argv, args.prompt) if args.prompt else runner(argv)
    body = _json(result)
    if result.returncode == 0 and isinstance(body, dict) and body.get("id"):
        row.deck_id, row.status, row.group = body["id"], body.get("status"), body.get("group")
        row.outcome = f"launched {_short(body['id'])}"
    elif isinstance(body, dict) and body.get("code") == "ALREADY_EXISTS":
        row.outcome = f"already open ({_error(result)})"
    else:
        row.outcome = f"failed: {_error(result)}"


def _refresh(row: Row, sid: str, context: str, runner=_run) -> None:
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    message = (
        f"Updated task context (Asana snapshot as of {stamp}); it supersedes the one "
        f"in your system prompt. No action needed beyond reading it.\n\n{context}"
    )
    result = runner(
        [
            "agent-deck",
            "session",
            "send",
            "--defer-if-busy",
            "--no-tag",
            "--message-file",
            "-",
            sid,
        ],
        message,
    )
    row.outcome = (
        f"refreshed {_short(sid)}"
        if result.returncode == 0
        else f"refresh failed: {_error(result)}"
    )


def prune(deck_rows: list[dict], group: str, spawn: bool, runner=_run, fetch=fetch) -> list[str]:
    """Stop and archive sessions in the group whose task is completed or gone."""
    lines = []
    for session in deck_rows:
        if not in_tree(session.get("group"), group) or session.get("archived"):
            continue
        gid = hints(session["id"], runner).get(GID_HINT)
        if not gid:
            continue
        try:
            done = bool(fetch(gid).get("completed"))
        except urllib.error.HTTPError as e:
            done = e.code == 404
        if not done:
            continue
        sid = _short(session["id"])
        if session.get("status") == "running":
            lines.append(f"{gid}\t{session.get('title')}\t→ kept (busy) {sid}")
        elif not spawn:
            lines.append(f"{gid}\t{session.get('title')}\t→ would prune {sid}")
        else:
            runner(["agent-deck", "session", "stop", session["id"]])
            result = runner(["agent-deck", "session", "archive", session["id"]])
            outcome = (
                f"pruned {sid}" if result.returncode == 0 else f"prune failed: {_error(result)}"
            )
            lines.append(f"{gid}\t{session.get('title')}\t→ {outcome}")
    return lines


# --- cli -----------------------------------------------------------------


def _location(task: dict) -> str:
    parts = [p for p in (task.get("project"), task.get("section")) if p]
    return "/".join(parts) or "—"


def main(
    argv: list[str] | None = None, *, runner=_run, fetch=fetch, search=search, env=os.environ
) -> int:
    parser = argparse.ArgumentParser(
        prog="task-sessions",
        description="Give each matching Asana task its own agent-deck Claude session.",
    )
    parser.add_argument("gids", nargs="*", help="task GIDs; skips /search entirely")
    parser.add_argument("--query", default="")
    parser.add_argument("--project")
    parser.add_argument("--due-before")
    parser.add_argument("--due-after")
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="caps /search only")
    parser.add_argument(
        "--spawn", action="store_true", help="actually launch/reopen; dry run otherwise"
    )
    parser.add_argument(
        "--group", help=f"agent-deck group (env TASK_SESSIONS_GROUP, default {DEFAULT_GROUP})"
    )
    parser.add_argument(
        "--parent",
        help=f"none|self|<session> (env TASK_SESSIONS_PARENT, default {DEFAULT_PARENT})",
    )
    parser.add_argument(
        "--inherit-group", action="store_true", help="put sessions in the parent's group"
    )
    parser.add_argument("--model")
    parser.add_argument("--idle-timeout", help="auto-stop after this long idle, e.g. 24h")
    parser.add_argument("--prompt", help="first message to send (default: START_PROMPT)")
    parser.add_argument("--prompt-file", help="read the first message from a file ('-' for stdin)")
    parser.add_argument(
        "--idle", action="store_true", help="send no first message; the session waits"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="resend fresh context to open sessions"
    )
    parser.add_argument(
        "--prune", action="store_true", help="archive sessions whose task is completed"
    )
    parser.add_argument("--json", action="store_true", help="emit rows as JSON")
    parser.add_argument("--print-context", metavar="GID", help="print one task's context block")
    args = parser.parse_args(argv)

    if args.print_context:
        task = fetch(args.print_context) | {"task_gid": args.print_context}
        print(context_block(task, ref_for(args.print_context)))
        return 0

    if args.prompt_file:
        args.prompt = (
            sys.stdin.read() if args.prompt_file == "-" else Path(args.prompt_file).read_text()
        )
    if args.idle:
        if args.prompt:
            raise SystemExit("task-sessions: --idle and --prompt conflict")
        args.prompt = None
    elif not args.prompt:
        args.prompt = START_PROMPT
    placement = resolve_placement(args, env)

    try:
        deck_rows = listing(runner)
    except DeckUnavailable as e:
        print(f"registry unavailable: {e} — nothing launched", file=sys.stderr)
        return 2
    warning = check_parent(placement, deck_rows)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)

    if args.prune:
        if not placement.group:
            raise SystemExit("task-sessions: --prune needs a group, not --inherit-group")
        try:
            for line in prune(deck_rows, placement.group, args.spawn, runner, fetch):
                print(line)
        except DeckUnavailable as e:
            print(f"registry unavailable: {e}", file=sys.stderr)
            return 2
        if (
            not args.gids
            and not args.query
            and not args.project
            and not args.due_before
            and not args.due_after
        ):
            return 0
        deck_rows = listing(runner)

    if args.gids:
        gids = list(dict.fromkeys(args.gids))  # dedupe, keep order (defect 5)
    else:
        hits = search(
            query=args.query,
            project=args.project,
            due_before=args.due_before,
            due_after=args.due_after,
            semantic=args.semantic or None,
            completed=False,
            limit=args.limit,
        )
        gids = list(dict.fromkeys(hit["task_gid"] for hit in hits[: args.limit]))
    if not gids:
        print("no tasks matched", file=sys.stderr)
        return 1

    rows: list[Row] = []
    for gid in gids:
        try:
            task = fetch(gid) | {"task_gid": gid}
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            task = {"task_gid": gid, "name": "?", "_error": str(e)}
        rows.append(Row(ref=ref_for(gid), gid=gid, task=task))

    try:
        find_sessions(rows, deck_rows, placement.group, runner)
    except DeckUnavailable as e:
        print(f"registry unavailable: {e} — nothing launched", file=sys.stderr)
        return 2

    if args.spawn and placement.group:
        warning = ensure_group(placement.group, runner)
        if warning:
            print(f"warning: {warning}", file=sys.stderr)

    for row in rows:  # serial on purpose: agent-deck thrashes on parallel launches
        if row.task.get("_error"):
            row.outcome = f"fetch failed: {row.task['_error']}"
        elif row.task.get("completed"):
            row.outcome = "skipped (completed)"
        else:
            try:
                act(row, args, placement, runner)
            except (DeckUnavailable, OSError) as e:
                row.outcome = f"failed: {e}"

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "ref": row.ref,
                        "gid": row.gid,
                        "name": row.task.get("name"),
                        "due": row.task.get("due_on") or row.task.get("due_at"),
                        "project": row.task.get("project"),
                        "section": row.task.get("section"),
                        "permalink": row.task.get("permalink_url"),
                        "outcome": row.outcome,
                        "deck_id": row.deck_id,
                        "status": row.status,
                        "group": row.group,
                        "attach": f"agent-deck session attach {_short(row.deck_id)}"
                        if row.deck_id
                        else None,
                    }
                    for row in rows
                ],
                indent=1,
            )
        )
    else:
        for row in rows:
            due = row.task.get("due_on") or row.task.get("due_at") or "—"
            print(
                f"{row.ref}\t{row.gid}\t{due}\t{_location(row.task)}\t{row.task.get('name')}"
                f"\t{row.task.get('permalink_url') or '—'}\t→ {row.outcome}"
            )

    if args.spawn:
        view = f"agent-deck -g {placement.group}" if placement.group else "agent-deck"
        print(
            "\nagent-deck session attach <id>   # needs a real terminal; Ctrl+Q detaches",
            file=sys.stderr,
        )
        print(f"{view}   # the view", file=sys.stderr)
    else:
        print("\ndry run — add --spawn to launch these", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

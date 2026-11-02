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
`asana_gid=<gid>`; a run leaves a task with a live session alone, reopens a
stopped one (same conversation), and launches the rest. The agent-deck
mechanics live in `deck-sessions` (~/src/scripts/deck-sessions, umbrella spec
~/src/scripts/docs/superpowers/specs/2026-11-02-deck-sessions-design.md); this
script builds one spec per task and maps the results back. A launch sends START_PROMPT (or `--prompt`) as the first
message, so the session starts working at once; `--idle` sends nothing and
the session waits at an empty prompt at zero cost. Reopened sessions are never
prompted. Design:
docs/superpowers/specs/2026-11-02-agent-deck-task-sessions-design.md and
docs/superpowers/specs/2026-11-02-task-sessions-on-deck-sessions-design.md.

Group: --group, else $TASK_SESSIONS_GROUP, else `tasks`.
Parent: --parent none|self|<session>, else $TASK_SESSIONS_PARENT, else
`conductor-tasks` (the tasks conductor) — or none when that session doesn't exist.

Stdlib only, Python >= 3.9: this runs from PATH under whatever `python3` is
first, not under the repo's venv. On a conductor's PATH that is macOS's
/usr/bin/python3 (3.9).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
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

ref_for = task_ref.ref


class DeckUnavailable(Exception):
    """deck-sessions could not read agent-deck's registry (or isn't installed)."""


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


def session_name(ref: str, name: str) -> str:
    full = f"{ref} {name}"
    return full if len(full) <= NAME_MAX else full[: NAME_MAX - 1] + "…"


# --- configuration -------------------------------------------------------


@dataclass
class Placement:
    group: str | None  # None = inherit the parent's group
    parent: str | None  # resolved session id/title; None = no parent
    parent_is_default: bool = False


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


# --- deck-sessions -------------------------------------------------------


def _deck(argv: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["deck-sessions", *argv], input=stdin, capture_output=True, text=True, check=False
    )


def _call_deck(argv: list[str], stdin: str | None, deck) -> str:
    """Run deck-sessions; return stdout. Forwards its warnings, raises on exit 2."""
    try:
        result = deck(argv, stdin)
    except OSError as e:
        raise DeckUnavailable(f"deck-sessions not on PATH ({e})")
    for line in (result.stderr or "").splitlines():
        try:
            body = json.loads(line)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict) and body.get("error"):
            raise DeckUnavailable(str(body["error"]).removeprefix("registry unavailable: "))
        print(line, file=sys.stderr)
    if result.returncode != 0:
        raise DeckUnavailable(f"deck-sessions exit {result.returncode}")
    return result.stdout


def spec_for(row: "Row", args, placement: Placement) -> dict:
    """The deck-sessions spec for one task (spec D2)."""
    permalink = row.task.get("permalink_url")
    return {
        "key_hint": GID_HINT,
        "key": row.gid,
        "title": row.title,
        "adopt_prefix": f"{row.ref} ",
        "context": context_block(row.task, row.ref),
        "context_path": str(CONTEXT_DIR / f"{row.gid}.md"),
        "cwd": str(REPO),
        "group": placement.group,
        "parent": placement.parent or "none",
        "parent_is_default": placement.parent_is_default,
        "hints": {"permalink": permalink} if permalink else {},
        "model": args.model,
        "idle_timeout": args.idle_timeout,
        "prompt": args.prompt,
        "reopen_prompt": None,
        "refresh": bool(args.refresh),
    }


def _short(session_id: str) -> str:
    return session_id[:SHORT_ID]


def outcome_text(result: dict) -> str:
    """deck-sessions' outcome in the words task-sessions has always printed."""
    outcome, detail, sid = result.get("outcome"), result.get("detail"), result.get("deck_id")
    short = _short(sid) if sid else ""
    if outcome == "launched":
        return f"launched {short}"
    if outcome == "already open":
        if not sid:
            return f"already open ({detail})"
        adopted = " · adopted" if result.get("adopted") else ""
        return f"already open {short} ({result.get('status')}){adopted}"
    if outcome in ("reopened", "refreshed", "would reopen", "would refresh"):
        return f"{outcome} {short}"
    if outcome == "duplicate":
        return "duplicate " + ",".join(_short(i) for i in (detail or "").split(","))
    if outcome == "would launch":
        return outcome
    if detail and detail.startswith("refresh failed"):
        return detail
    return f"failed: {detail}"


@dataclass
class Row:
    ref: str
    gid: str
    task: dict
    outcome: str = ""
    deck_id: str | None = None
    status: str | None = None
    group: str | None = None

    @property
    def title(self) -> str:
        return session_name(self.ref, self.task["name"])


def prune(group: str, spawn: bool, deck=_deck, fetch=fetch) -> list[str]:
    """Stop and archive sessions in the group whose task is completed or gone."""
    rows = json.loads(
        _call_deck(["sessions", "--key-hint", GID_HINT, "--group", group], None, deck)
    )
    done = []
    for session in rows:
        if session.get("archived"):
            continue
        try:
            completed = bool(fetch(session["key"]).get("completed"))
        except urllib.error.HTTPError as e:
            completed = e.code == 404
        if completed:
            done.append(session)
    if not done:
        return []
    by_id = {s["deck_id"]: s for s in done}
    argv = ["archive"] + (["--apply"] if spawn else []) + [s["deck_id"] for s in done]
    lines = []
    for line in _call_deck(argv, None, deck).splitlines():
        result = json.loads(line)
        session = by_id.get(result["deck_id"], {})
        sid = _short(result["deck_id"])
        outcome = {
            "archived": f"pruned {sid}",
            "kept (busy)": f"kept (busy) {sid}",
            "would archive": f"would prune {sid}",
        }.get(result["outcome"], f"prune failed: {result.get('detail')}")
        lines.append(f"{session.get('key')}\t{session.get('title')}\t→ {outcome}")
    return lines


# --- cli -----------------------------------------------------------------


def _location(task: dict) -> str:
    parts = [p for p in (task.get("project"), task.get("section")) if p]
    return "/".join(parts) or "—"


def main(
    argv: list[str] | None = None, *, deck=_deck, fetch=fetch, search=search, env=os.environ
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

    if args.prune:
        if not placement.group:
            raise SystemExit("task-sessions: --prune needs a group, not --inherit-group")
        try:
            for line in prune(placement.group, args.spawn, deck, fetch):
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

    if args.gids:
        gids = list(dict.fromkeys(args.gids))  # dedupe, keep order
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

    for row in rows:
        if row.task.get("_error"):
            row.outcome = f"fetch failed: {row.task['_error']}"
        elif row.task.get("completed"):
            row.outcome = "skipped (completed)"
    pending = [row for row in rows if not row.outcome]

    if pending:
        specs = "".join(json.dumps(spec_for(row, args, placement)) + "\n" for row in pending)
        try:
            out = _call_deck(["ensure"] + (["--apply"] if args.spawn else []), specs, deck)
        except DeckUnavailable as e:
            print(f"registry unavailable: {e} — nothing launched", file=sys.stderr)
            return 2
        results = [json.loads(line) for line in out.splitlines() if line.strip()]
        if len(results) != len(pending):
            print("deck-sessions returned the wrong number of results", file=sys.stderr)
            return 2
        for row, result in zip(pending, results):
            detail = result.get("detail") or ""
            if result.get("outcome") == "failed" and detail.startswith("parent "):
                raise SystemExit(f"task-sessions: {detail}")
            row.outcome = outcome_text(result)
            row.deck_id, row.status = result.get("deck_id"), result.get("status")
            row.group = result.get("group")

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

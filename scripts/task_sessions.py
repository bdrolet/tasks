#!/usr/bin/env python3
"""One background Claude Code session per Asana task.

Picks tasks the way a listing does — the same `/search` filters the
`task-lister` agent uses — and gives each one its own background session,
preloaded with that task's full context (notes, subtasks, comments) via
`--append-system-prompt`. `claude agents` is the view; `claude attach <id>`
opens one to work by hand.

`scripts/link-skills.sh` symlinks this onto PATH as `task-sessions`.

    task-sessions --due-before 2026-09-18            # dry run: what it would spawn
    task-sessions --due-before 2026-09-18 --spawn    # launch them
    task-sessions 1218118170820306 --spawn           # explicit GIDs
    task-sessions --print-context 1218118170820306   # the block a session would boot with

Sessions are spawned with no prompt, so they idle at zero token cost until
someone attaches. A task that already has a live session is left alone:
`~/.claude/task-sessions.json` maps GID to session id, reconciled against
`claude agents --json` on every run, so a session you removed frees its task
and a session still running blocks a duplicate.

Stdlib only — this runs from PATH under whatever `python3` is first, not
under the repo's venv.
"""

import argparse
import functools
import json
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_ref

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = Path.home() / ".claude" / "task-sessions.json"
API_BASE = "https://tasks-api.drolet.cloud"
NAME_MAX = 60
SHORT_ID = 8
DEFAULT_LIMIT = 10

ref_for = task_ref.ref


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
    """The markdown a spawned session carries in its system prompt."""
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


# --- spawning ------------------------------------------------------------


def spawn_argv(name: str, context: str) -> list[str]:
    # No prompt argument: the session boots with the context in its system
    # prompt and waits, rather than starting a turn nobody asked for.
    #
    # No --session-id either. `claude --bg` picks the id itself and warns
    # "--bg manages the session id; ignoring --session-id" when handed one,
    # so the id has to be read back out of its output.
    return ["claude", "--bg", "-n", name, "--append-system-prompt", context]


def parse_session_id(output: str) -> str | None:
    """Pull the short id out of `backgrounded · ed1b7ea6 · <name>`."""
    for line in output.splitlines():
        if line.strip().startswith("backgrounded") and "·" in line:
            parts = [part.strip() for part in line.split("·")]
            if len(parts) >= 2 and parts[1]:
                return parts[1]
    return None


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False)


def spawn(name: str, context: str, runner=_run) -> str | None:
    """Start a background session; return the short id `claude attach` takes."""
    result = runner(spawn_argv(name, context))
    return parse_session_id(result.stdout) if result.returncode == 0 else None


def live_session_ids(runner=_run) -> set[str]:
    """Short ids of every session `claude agents` currently lists."""
    try:
        listing = json.loads(runner(["claude", "agents", "--json", "--all"]).stdout or "[]")
    except (OSError, json.JSONDecodeError):
        return set()
    ids = set()
    for session in listing:
        short = session.get("id") or (session.get("sessionId") or "")[:SHORT_ID]
        if short:
            ids.add(short)
    return ids


# --- state ---------------------------------------------------------------


def load_state(path: Path = STATE_PATH) -> dict[str, str]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))


def reconcile(state: dict[str, str], live: set[str]) -> dict[str, str]:
    """Forget GIDs whose session is gone, so the task is eligible again."""
    return {gid: sid for gid, sid in state.items() if sid in live}


# --- planning ------------------------------------------------------------


@dataclass
class Row:
    ref: str
    gid: str
    due: str
    name: str
    action: str
    session_id: str | None


def plan(tasks: list[dict], state: dict[str, str]) -> list[Row]:
    refs = task_ref.assign([t["task_gid"] for t in tasks])
    rows = []
    for task in tasks:
        gid = task["task_gid"]
        existing = state.get(gid)
        rows.append(
            Row(
                ref=refs[gid],
                gid=gid,
                due=task.get("due_on") or "—",
                name=task["name"],
                action="already open" if existing else "spawn",
                session_id=existing,
            )
        )
    return rows


# --- cli -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="task-sessions",
        description="Give each matching Asana task its own background Claude session.",
    )
    parser.add_argument("gids", nargs="*", help="task GIDs; skips /search entirely")
    parser.add_argument("--query", default="")
    parser.add_argument("--project")
    parser.add_argument("--due-before")
    parser.add_argument("--due-after")
    parser.add_argument("--semantic", action="store_true")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--spawn", action="store_true", help="actually launch sessions")
    parser.add_argument("--print-context", metavar="GID", help="print one task's context block")
    args = parser.parse_args(argv)

    if args.print_context:
        print(context_block(fetch(args.print_context), ref_for(args.print_context)))
        return 0

    if args.gids:
        hits = [{"task_gid": gid} for gid in args.gids]
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
    if not hits:
        print("no tasks matched", file=sys.stderr)
        return 1

    # One fetch per task: /search returns a summary, and a session wants the
    # description, subtasks and comments the fetch carries.
    tasks = [fetch(hit["task_gid"]) | {"task_gid": hit["task_gid"]} for hit in hits[: args.limit]]

    state = reconcile(load_state(), live_session_ids())
    rows = plan(tasks, state)
    by_gid = {task["task_gid"]: task for task in tasks}

    for row in rows:
        if args.spawn and row.action == "spawn":
            context = context_block(by_gid[row.gid], row.ref)
            session_id = spawn(session_name(row.ref, row.name), context)
            if session_id:
                state[row.gid] = session_id
                row.session_id, row.action = session_id, "spawned"
            else:
                row.action = "spawn failed"
        outcome = f"{row.action} {row.session_id}" if row.session_id else row.action
        print(f"{row.ref}\t{row.gid}\t{row.due}\t{row.name}\t→ {outcome}")

    if args.spawn:
        save_state(STATE_PATH, state)
        print("\nclaude agents          # the view", file=sys.stderr)
        print("claude attach <id>     # work one (needs a real terminal)", file=sys.stderr)
    else:
        print("\ndry run — add --spawn to launch these", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

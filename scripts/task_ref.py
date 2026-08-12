#!/usr/bin/env python3
"""Three-character base36 reference numbers for Asana task GIDs.

A ref is a deterministic hash of the GID, so the same task carries the same
ref across listings without anything being stored. Used by the `task-lister`
agent to give each row a handle short enough to say out loud.

`scripts/link-skills.sh` symlinks this onto PATH as `task-ref`, which is how
the skills and the task-lister agent invoke it.

Two modes:

    curl ... /search       | task-ref     # annotate a search response
    curl ... /tasks/<gid>  | task-ref     # annotate that task's subtasks
    task-ref 1217130164408154 ...         # ref for specific GIDs

Annotate mode reads a `/search` response (a `results` array) or a single-task
fetch (its `subtasks` array) on stdin and writes TSV to stdout, one row per
task, in the order the API returned them:

    ref  gid  due_on  name  location

`location` is `project/section`, or `subtask of <parent>` for subtask hits,
or `—` when the API omits both — as it does for the subtasks of a fetched
task, which belong to their parent rather than to a section.

Collisions: 36**3 = 46,656 refs, so two tasks in one listing collide about
0.6% of the time at 25 results (see tests/test_task_ref.py, which pins the
rate). When it happens the loser is rehashed with a salt until it lands
somewhere free. Resolution is deterministic for a given set of GIDs (they are
salted in sorted order), but it depends on which tasks share the listing — a
bumped task can show a different ref in a listing its twin isn't part of.
Refs are handles for a conversation, not identifiers: every write path still
takes the GID.
"""

import hashlib
import json
import sys

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
WIDTH = 3
SPACE = len(ALPHABET) ** WIDTH


def ref(gid: str, salt: int = 0) -> str:
    """Base36 ref for a GID. `salt` picks an alternate on collision."""
    key = gid if salt == 0 else f"{gid}#{salt}"
    n = int(hashlib.sha256(key.encode()).hexdigest(), 16) % SPACE
    out = ""
    for _ in range(WIDTH):
        n, i = divmod(n, len(ALPHABET))
        out = ALPHABET[i] + out
    return out


def assign(gids: list[str]) -> dict[str, str]:
    """Map each GID to a ref, unique within this set.

    Sorted order makes the tiebreak deterministic: the same set of GIDs always
    produces the same assignment, whatever order they arrived in.
    """
    taken: dict[str, str] = {}
    for gid in sorted(gids):
        salt = 0
        while (candidate := ref(gid, salt)) in taken:
            salt += 1
        taken[candidate] = gid
    return {gid: r for r, gid in taken.items()}


def location(result: dict) -> str:
    if result.get("parent"):
        return f"subtask of {result['parent']}"
    project = result.get("project") or "—"
    section = result.get("section")
    return f"{project}/{section}" if section else project


def main() -> int:
    if len(sys.argv) > 1:
        refs = assign(sys.argv[1:])
        for gid in sys.argv[1:]:
            print(f"{refs[gid]}\t{gid}")
        return 0

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"task_ref: stdin is not JSON ({exc})", file=sys.stderr)
        return 1

    # A /search response carries `results`; a single-task fetch carries
    # `subtasks`. Both are lists of tasks with a `task_gid`, so either works.
    results = payload.get("results")
    if results is None:
        results = payload.get("subtasks")
    if results is None:
        print(
            "task_ref: no 'results' or 'subtasks' key — is this a /search "
            "or /tasks/<gid> response?",
            file=sys.stderr,
        )
        return 1

    refs = assign([r["task_gid"] for r in results])
    for r in results:
        gid = r["task_gid"]
        due = r.get("due_on") or "—"
        print(f"{refs[gid]}\t{gid}\t{due}\t{r['name']}\t{location(r)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

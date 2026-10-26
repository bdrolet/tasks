#!/usr/bin/env python3
"""Print a task and every subtask beneath it as a tree.

    task-tree <gid>                 # whole tree, refs on every row
    task-tree <gid> --today 2026-10-26

The tasks-api fetch (`GET /tasks/<gid>`) lists subtasks one level deep, so
this walks the tree a level at a time, fetching each level's tasks in
parallel. Siblings sort by due date (undated last); a completed task is
`[x]`, and an open task past its due date is flagged `← overdue`:

    [P2] Host my birthday party at Carter  (Sat Dec 5) · Ben's Board/Soon
    ├── [x] 4kx  [P2] Pick the party date            Oct 9
    ├── [ ] h2p  [P3] Plan the party setup           Oct 24  ← overdue
    └── [ ] 0eh  [P3] Do the final staging           Dec 4

    refs: 4kx=1218335741122258  h2p=1218335741324401  0eh=1218335741041047

Refs are scripts/task_ref.py refs, assigned across the whole tree so the
closing map resolves every row. Stdlib only — this runs from PATH under
whatever python3 is first. `scripts/link-skills.sh` symlinks this onto PATH
as `task-tree`.
"""

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import task_ref  # noqa: E402

API_BASE = "https://tasks-api.drolet.cloud"
# Asana nests subtasks without limit; a tree deeper than this is a cycle or
# a mistake, not a plan.
MAX_DEPTH = 8


def _token() -> str:
    try:
        return subprocess.run(
            ["gcloud", "auth", "print-identity-token"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as e:
        raise SystemExit(f"task-tree: gcloud auth print-identity-token failed: {e}")


def _fetch(gid: str, token: str) -> dict:
    request = urllib.request.Request(
        f"{API_BASE}/tasks/{gid}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"task-tree: GET /tasks/{gid} -> {e.code}: {e.read().decode(errors='replace')}")


def walk(root_gid: str, fetch) -> tuple[dict, dict[str, list[dict]]]:
    """Fetch the root and every descendant. Returns (root, gid -> child rows).

    `fetch(gid) -> task` is injected so tests can run without the API.
    """
    root = fetch(root_gid)
    children: dict[str, list[dict]] = {root_gid: root.get("subtasks") or []}
    level = [s["task_gid"] for s in children[root_gid]]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for _ in range(MAX_DEPTH):
            if not level:
                break
            fetched = list(pool.map(fetch, level))
            for gid, task in zip(level, fetched):
                children[gid] = task.get("subtasks") or []
            level = [s["task_gid"] for gid in level for s in children[gid]]
    return root, children


def _date(due: str | None, today: date, weekday: bool = False) -> str:
    if not due:
        return "—"
    d = date.fromisoformat(due)
    out = f"{d:%b} {d.day}"
    if weekday:
        out = f"{d:%a} {out}"
    if d.year != today.year:
        out += f" {d.year}"
    return out


def _sorted(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (r.get("due_on") is None, r.get("due_on") or "", r["name"]))


def render(root: dict, children: dict[str, list[dict]], today: date) -> str:
    gids = [r["task_gid"] for rows in children.values() for r in rows]
    refs = task_ref.assign(gids)

    # (prefix, row) in display order, so the name column can be padded to
    # one width across every depth.
    lines: list[tuple[str, dict]] = []

    def visit(gid: str, indent: str) -> None:
        rows = _sorted(children.get(gid, []))
        for i, row in enumerate(rows):
            last = i == len(rows) - 1
            box = "[x]" if row.get("completed") else "[ ]"
            lines.append((f"{indent}{'└── ' if last else '├── '}{box} {refs[row['task_gid']]}  ", row))
            visit(row["task_gid"], indent + ("    " if last else "│   "))

    visit(root["task_gid"] if "task_gid" in root else root["gid"], "")

    if root.get("parent"):
        where = f"subtask of {root['parent']['name']}"
    else:
        where = "/".join(p for p in (root.get("project"), root.get("section")) if p) or "—"
    head = root["name"]
    if root.get("completed"):
        head = f"✓ {head}"
    if root.get("due_on"):
        head += f"  ({_date(root['due_on'], today, weekday=True)})"
    out = [f"{head} · {where}"]

    width = max((len(p) + len(r["name"]) for p, r in lines), default=0)
    for prefix, row in lines:
        text = f"{prefix}{row['name']}".ljust(width) + f"  {_date(row.get('due_on'), today)}"
        if not row.get("completed") and row.get("due_on") and row["due_on"] < today.isoformat():
            text += "  ← overdue"
        out.append(text.rstrip())

    if not lines:
        out.append("(no subtasks)")
    else:
        out.append("")
        out.append("refs: " + "  ".join(f"{refs[r['task_gid']]}={r['task_gid']}" for _, r in lines))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="task-tree", description=__doc__.splitlines()[0])
    parser.add_argument("gid", help="root task GID (never a ref)")
    parser.add_argument("--today", help="YYYY-MM-DD for the overdue flag (default: today)")
    args = parser.parse_args(argv)

    today = date.fromisoformat(args.today) if args.today else date.today()
    token = _token()
    root, children = walk(args.gid, lambda gid: _fetch(gid, token))
    root.setdefault("gid", args.gid)
    print(render(root, children, today))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

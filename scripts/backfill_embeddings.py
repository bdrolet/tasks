#!/usr/bin/env python3
"""Seed or heal the task_index semantic-search corpus from Asana.

Idempotent: unchanged content never re-embeds (hash-gated); rows with a
missing vector are retried. Run after deploy, or anytime to heal drift:

  scripts/fetch-env.sh   # once, for .env
  .venv/bin/python scripts/backfill_embeddings.py --dry-run
  .venv/bin/python scripts/backfill_embeddings.py

Needs .env (Asana + DB) and gcloud ADC for Vertex
(`gcloud auth application-default login`).
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.asana as asana
from clients.db import get_conn
from repo import task_index as repo_index
from services import task_index


def all_tasks() -> list[dict]:
    """Every task in the workspace, incl. completed and subtasks — the same
    sweep the substring search path does."""
    projects = asana.list_projects()
    with ThreadPoolExecutor(max_workers=8) as pool:
        per_project = list(
            pool.map(lambda p: asana.list_project_tasks(p["gid"], only_open=False), projects)
        )
    tasks = [t for batch in per_project for t in batch]
    tasks += asana.list_my_tasks(only_open=False)
    with_subs = [t for t in tasks if t.get("num_subtasks")]
    if with_subs:
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(lambda t: asana.get_subtasks(t["gid"]), with_subs))
        for batch in batches:
            tasks.extend(batch)
    seen: set[str] = set()
    unique = []
    for t in tasks:
        if t["gid"] not in seen:
            seen.add(t["gid"])
            unique.append(t)
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, don't write or embed")
    args = parser.parse_args()

    tasks = all_tasks()
    print(f"{len(tasks)} tasks listed from Asana")

    embedded = skipped = failed = 0
    with get_conn() as conn:
        for i, t in enumerate(tasks, 1):
            title, notes = t.get("name") or "", t.get("notes") or ""
            if args.dry_run:
                state = repo_index.get_state(conn, t["gid"])
                chash = task_index.content_hash(title, notes)
                if state is None or state["content_hash"] != chash or not state["has_embedding"]:
                    embedded += 1
                else:
                    skipped += 1
                continue
            if task_index.index_task_dict(conn, t):
                embedded += 1
            else:
                # skipped (hash match) or embed failure — index_task_dict logged it
                skipped += 1
            if i % 50 == 0:
                conn.commit()
                print(f"  {i}/{len(tasks)} …")
        if not args.dry_run:
            conn.commit()

    verb = "would embed" if args.dry_run else "embedded"
    print(f"done: {verb} {embedded}, skipped {skipped}, failed {failed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"backfill failed: {e}", file=sys.stderr)
        sys.exit(1)

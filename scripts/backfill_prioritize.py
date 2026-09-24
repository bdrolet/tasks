#!/usr/bin/env python3
"""Publish task_changed for every open task in every managed project (plus
their open subtasks) so the prioritizer builds its tables from scratch.
Also the fix for a long outage. Dry-run by default.

  (set -a; source .env; set +a; .venv/bin/python scripts/backfill_prioritize.py [--publish])
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.asana as asana  # noqa: E402
import clients.pubsub as pubsub  # noqa: E402
from services import managed_projects  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true", help="actually publish")
    args = parser.parse_args()
    projects = managed_projects.gids()
    if not projects:
        raise SystemExit(
            "backfill_prioritize: ASANA_MANAGED_PROJECTS is unset or empty — nothing to "
            "backfill (run scripts/fetch-env.sh, or set it in .env)"
        )
    gids: list[str] = []
    for project_gid in sorted(projects):
        for task in asana.list_project_tasks(
            project_gid, only_open=True, opt_fields=asana.HEAL_OPT_FIELDS
        ):
            gids.append(task["gid"])
            if task.get("num_subtasks"):
                gids += [
                    s["gid"] for s in asana.get_subtasks(task["gid"]) if not s.get("completed")
                ]
    print(f"{len(gids)} task(s) across {len(projects)} project(s)")
    if not args.publish:
        print("dry run — pass --publish to send")
        return
    for gid in gids:
        pubsub.publish(
            pubsub.TASK_EVENTS, {"kind": "task_changed", "gid": gid, "source": "backfill"}
        )
    print("published")


if __name__ == "__main__":
    main()

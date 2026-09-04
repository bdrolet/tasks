#!/usr/bin/env python3
"""Local run of the due-day digest against REAL Asana and REAL schedule-api.

Usage (from repo root, after scripts/fetch-env.sh):
    .venv/bin/python scripts/test-digest.py --dry-run      # print the plan, write nothing
    .venv/bin/python scripts/test-digest.py                # apply: creates/updates/deletes REAL events

--dry-run lists candidates, routes them, and prints desired events and the
diff against due_day_events without calling Claude or schedule-api. The real
run is exactly what POST /digest does with force=true; it shares the
digest_state row with production, so don't run it while the scheduler is
mid-rebuild (the job runs at :00, :10, :20 … — start between ticks).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.db import get_conn
from handlers import due_digest as h
from models.digest import DigestTask
from repo import due_digest as repo
from services import due_digest as dd
from services import task_bullets as tb


def dry_run() -> None:
    today = dd.today_local()
    routing = h._routing()
    tasks = [
        DigestTask(
            gid=t["gid"],
            name=t["name"],
            permalink_url=t.get("permalink_url") or "",
            due_on=t["due_on"],
            calendar_id=dd.route(t, **routing),
            points=tb.fallback_points(t.get("html_notes") or ""),
            links=tb.parse_links(t.get("html_notes") or ""),
        )
        for t in h._list_candidates()
        if dd.in_window(t, today)
    ]
    desired = dd.build_events(tasks)
    with get_conn() as conn:
        stored = repo.list_events(conn, since=today)
    plan = dd.plan(desired, stored, today)
    print(f"today={today} tasks_in_window={len(tasks)} desired_events={len(desired)}")
    for (day, cal), ev in sorted(desired.items()):
        print(f"  {day} {cal}: {ev.title}")
        for s in ev.sections:
            print(f"    - {s['title']}  ({len(s['points'])} pts, {len(s['links'])} links)")
    print(f"plan: create={len(plan.creates)} update={len(plan.updates)} delete={len(plan.deletes)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
        return
    print(json.dumps(h.run(force=True), indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# scripts/test-review.py
"""Render the weekly review from the live database without posting.

  (set -a; source .env; set +a; .venv/bin/python scripts/test-review.py --dry-run)

Without --dry-run it posts the comment on the standing task, exactly as the
Monday scheduler does."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.db import get_conn
from handlers import weekly_review
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import review
from services.due_digest import today_local


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dry_run:
        print(weekly_review.run())
        return 0
    today = today_local()
    with get_conn() as conn:
        strategy = repo_goals.load_snapshot(conn)
        states = repo_goals.get_states(conn, today) or repo_goals.get_states(
            conn, today.fromordinal(today.toordinal() - 1)
        )
        body = review.build(
            strategy, states, repo.list_scores(conn), repo_sup.list_necessity(conn), today
        )
    print(review.render(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())

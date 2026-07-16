"""Local smoke test: build an email_classified event and run the handler
directly — exercises the full path: policy → Claude enrichment → Asana create
→ section placement → DB row.

Usage (from repo root, after scripts/fetch-env.sh):
    .venv/bin/python scripts/test-task-create.py [--category respond|urgent|ignore]

Creates a REAL Asana task (except --category ignore, which must no-op) and
spends real Claude tokens. Uses a fresh UUID for message_id so Asana's
duplicate external.gid check never trips on re-runs.
"""

import argparse
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from handlers import task_create
from models.events import EmailClassifiedEvent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--category", choices=["review", "respond", "urgent", "ignore"], default="review"
    )
    args = parser.parse_args()

    event: EmailClassifiedEvent = {
        "event": "email_classified",
        "message_id": str(uuid.uuid4()),
        "category": args.category,
        "importance": "P1",
        "confidence": 0.99,
        "subject": f"[test] tasks smoke test {datetime.now(UTC):%H:%M:%S}",
        "sender": "test@drolet.cloud",
        "sender_display": "Smoke Test",
        "received_at": datetime.now(UTC).isoformat(),
        "tags": [],
        "reasoning": "Local smoke test via scripts/test-task-create.py",
        "body": (
            "This task was created by the local smoke test — safe to delete. "
            "Please review the tasks repo setup and confirm the deployment checklist "
            "is complete by 2026-08-01."
        ),
        "body_html": '<p>See <a href="https://github.com/bdrolet/tasks">the tasks repo</a></p>',
        "web_link": None,
    }
    task_create.handle(event)
    print(
        f"Handler ran for category={args.category} (message_id={event['message_id']}). "
        "Expect: a task in the matching section with Claude key points, a repo link, and a "
        "due date around 2026-08-01 — or no task at all for --category ignore."
    )


if __name__ == "__main__":
    main()

"""Explicit-deadline extraction (migrated from inbox services/deadline.py).
Called only for P0/P1 events — see handlers/task_create.py."""

import logging
from datetime import date

import clients.claude as claude
from models.events import EmailClassifiedEvent

logger = logging.getLogger(__name__)


def extract_deadline(event: EmailClassifiedEvent) -> str | None:
    """Return ISO 8601 due date if the email states an explicit deadline, else None."""
    today = date.today().isoformat()
    prompt = (
        f"Today is {today}.\n"
        "Does the following email contain an explicit deadline or due date?\n"
        "If yes, reply with ONLY the date in ISO 8601 format (YYYY-MM-DD).\n"
        "If no explicit deadline is stated, reply with ONLY the word null.\n\n"
        f"Subject: {event['subject']}\n\n"
        f"{(event['body'] or '')[:1000]}"
    )
    raw = claude.extract(prompt)
    result = None if raw.lower() == "null" else raw
    logger.debug("deadline extraction for message_id=%s → %s", event["message_id"], result)
    return result

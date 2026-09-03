"""suppressed_emails — the audit trail for gate-2 decisions. Takes an open
connection. Best-effort by contract: callers log and move on if this fails."""

import json
from typing import Any


def exists(conn: Any, message_id: str) -> bool:
    """Has this message_id already been recorded? Used to guard against
    posting a duplicate Asana comment on Pub/Sub redelivery — the insert
    below is idempotent on message_id, but an Asana story has no
    idempotency key of its own."""
    row = conn.execute(
        "SELECT 1 FROM suppressed_emails WHERE message_id = %s", (message_id,)
    ).fetchone()
    return row is not None


def insert(
    conn: Any,
    *,
    message_id: str,
    category: str,
    importance: str,
    subject: str | None,
    sender: str | None,
    reason: str,
    source: str,
    related_task_gid: str | None,
    evidence: list,
) -> None:
    conn.execute(
        """
        INSERT INTO suppressed_emails
            (message_id, category, importance, subject, sender, reason, source,
             related_task_gid, evidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (message_id) DO NOTHING
        """,
        (
            message_id,
            category,
            importance,
            subject,
            sender,
            reason,
            source,
            related_task_gid,
            json.dumps(evidence),
        ),
    )

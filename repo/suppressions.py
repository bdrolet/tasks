"""suppressed_emails — the audit trail for gate-2 decisions. Takes an open
connection. Best-effort by contract: callers log and move on if this fails."""

import json
from typing import Any


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

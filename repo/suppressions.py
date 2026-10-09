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
    web_link: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO suppressed_emails
            (message_id, category, importance, subject, sender, reason, source,
             related_task_gid, evidence, web_link)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
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
            web_link,
        ),
    )


def get(conn: Any, message_id: str) -> dict | None:
    return conn.execute(
        "SELECT message_id, category, importance, subject, sender, reason, source, web_link, "
        "restored_at, restored_task_gid, created_at FROM suppressed_emails WHERE message_id = %s",
        (message_id,),
    ).fetchone()


def list_necessity(conn: Any, *, limit: int = 100) -> list[dict]:
    return conn.execute(
        "SELECT message_id, subject, sender, reason, web_link, created_at, restored_at, restored_task_gid "
        "FROM suppressed_emails WHERE source = 'necessity' ORDER BY created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def mark_restored(conn: Any, message_id: str, task_gid: str) -> bool:
    cur = conn.execute(
        "UPDATE suppressed_emails SET restored_at = now(), restored_task_gid = %s "
        "WHERE message_id = %s AND restored_at IS NULL",
        (task_gid, message_id),
    )
    return cur.rowcount == 1


def restore_rates(conn: Any, settle_days: int) -> list[dict]:
    """Per confidence band stored in evidence[0].necessity_confidence (D14):
    restored count, settled-unrestored count, pending count."""
    return conn.execute(
        """
        SELECT COALESCE(evidence->0->>'necessity_confidence', 'unknown') AS band,
               COUNT(*) FILTER (WHERE restored_at IS NOT NULL) AS restored,
               COUNT(*) FILTER (WHERE restored_at IS NULL AND created_at < now() - make_interval(days => %s)) AS settled,
               COUNT(*) FILTER (WHERE restored_at IS NULL AND created_at >= now() - make_interval(days => %s)) AS pending
        FROM suppressed_emails WHERE source = 'necessity' GROUP BY 1
        """,
        (settle_days, settle_days),
    ).fetchall()

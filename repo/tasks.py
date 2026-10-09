import json
from typing import Any


def insert(
    conn: Any,
    *,
    task_gid: str,
    message_id: str,
    category: str,
    importance: str,
    serves_estimated: dict | None = None,
) -> None:
    """Record a created task. Idempotent on message_id (Pub/Sub redelivery)."""
    conn.execute(
        """
        INSERT INTO tasks (task_gid, message_id, category, importance, serves_estimated)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (message_id) DO NOTHING
        """,
        (
            task_gid,
            message_id,
            category,
            importance,
            json.dumps(serves_estimated) if serves_estimated is not None else None,
        ),
    )


def necessity_rows(conn: Any) -> list[dict]:
    """Gate-2 serves drafts recorded at creation, shaped like
    repo.prioritize.necessity_rows (source tells the two apart)."""
    return conn.execute(
        """
        SELECT t.task_gid, t.serves_estimated, f.tags, o.overrides, e.strategy_hash,
               'gate2' AS source
        FROM tasks t
        JOIN task_facts f USING (task_gid)
        LEFT JOIN task_overrides o USING (task_gid)
        LEFT JOIN task_enrichment e USING (task_gid)
        WHERE t.serves_estimated IS NOT NULL
        """
    ).fetchall()


def get_gid_by_message(conn: Any, message_id: str) -> str | None:
    row = conn.execute("SELECT task_gid FROM tasks WHERE message_id = %s", (message_id,)).fetchone()
    return row["task_gid"] if row else None


def mark_completed(conn: Any, task_gid: str) -> None:
    conn.execute(
        "UPDATE tasks SET completed_at = now() WHERE task_gid = %s AND completed_at IS NULL",
        (task_gid,),
    )


def mark_escalated(conn: Any, task_gid: str) -> None:
    conn.execute("UPDATE tasks SET escalated_at = now() WHERE task_gid = %s", (task_gid,))


def was_escalated(conn: Any, task_gid: str) -> bool:
    row = conn.execute(
        "SELECT escalated_at FROM tasks WHERE task_gid = %s AND escalated_at IS NOT NULL",
        (task_gid,),
    ).fetchone()
    return row is not None


def email_context_by_gids(conn: Any, task_gids: list[str]) -> dict[str, dict]:
    """Email metadata for email-derived tasks, keyed by task_gid. Tasks not
    created from emails simply won't appear in the result."""
    if not task_gids:
        return {}
    placeholders = ",".join(["%s"] * len(task_gids))
    rows = conn.execute(
        f"SELECT task_gid, message_id, category, importance FROM tasks"
        f" WHERE task_gid IN ({placeholders})",
        tuple(task_gids),
    ).fetchall()
    return {r["task_gid"]: r for r in rows}

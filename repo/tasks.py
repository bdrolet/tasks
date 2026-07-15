from typing import Any


def insert(conn: Any, *, task_gid: str, message_id: str, category: str, importance: str) -> None:
    """Record a created task. Idempotent on message_id (Pub/Sub redelivery)."""
    conn.execute(
        """
        INSERT INTO tasks (task_gid, message_id, category, importance)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (message_id) DO NOTHING
        """,
        (task_gid, message_id, category, importance),
    )


def get_gid_by_message(conn: Any, message_id: str) -> str | None:
    row = conn.execute(
        "SELECT task_gid FROM tasks WHERE message_id = %s", (message_id,)
    ).fetchone()
    return row["task_gid"] if row else None


def mark_completed(conn: Any, task_gid: str) -> None:
    conn.execute(
        "UPDATE tasks SET completed_at = now() WHERE task_gid = %s AND completed_at IS NULL",
        (task_gid,),
    )


def mark_escalated(conn: Any, task_gid: str) -> None:
    conn.execute(
        "UPDATE tasks SET escalated_at = now() WHERE task_gid = %s", (task_gid,)
    )


def was_escalated(conn: Any, task_gid: str) -> bool:
    row = conn.execute(
        "SELECT escalated_at FROM tasks WHERE task_gid = %s AND escalated_at IS NOT NULL",
        (task_gid,),
    ).fetchone()
    return row is not None

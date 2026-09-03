"""due_day_events / task_bullets / digest_state — the due-day digest's state.
Takes an open connection. Unlike the rest of repo/, due_day_events is NOT
best-effort: handlers/due_digest.py skips a rebuild it cannot read."""

import json
from datetime import date
from typing import Any


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return json.loads(value) if isinstance(value, str) else list(value)


def _iso_day(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def list_events(conn: Any, *, since: date) -> list[dict]:
    rows = conn.execute(
        "SELECT day, calendar_id, event_id, content_hash, task_gids "
        "FROM due_day_events WHERE day >= %s ORDER BY day, calendar_id",
        (since,),
    ).fetchall()
    return [
        {
            "day": _iso_day(r["day"]),
            "calendar_id": r["calendar_id"],
            "event_id": r["event_id"],
            "content_hash": r["content_hash"],
            "task_gids": _as_list(r["task_gids"]),
        }
        for r in rows
    ]


def upsert_event(
    conn: Any, *, day: str, calendar_id: str, event_id: str, content_hash: str, task_gids: list[str]
) -> None:
    conn.execute(
        """
        INSERT INTO due_day_events (day, calendar_id, event_id, content_hash, task_gids)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (day, calendar_id) DO UPDATE SET
            event_id = EXCLUDED.event_id,
            content_hash = EXCLUDED.content_hash,
            task_gids = EXCLUDED.task_gids,
            updated_at = now()
        """,
        (day, calendar_id, event_id, content_hash, json.dumps(task_gids)),
    )


def delete_event(conn: Any, *, day: str, calendar_id: str) -> None:
    conn.execute(
        "DELETE FROM due_day_events WHERE day = %s AND calendar_id = %s", (day, calendar_id)
    )


def prune_events(conn: Any, *, before: date) -> None:
    conn.execute("DELETE FROM due_day_events WHERE day < %s", (before,))


def get_bullets(conn: Any, gid: str) -> dict | None:
    row = conn.execute(
        "SELECT content_hash, bullets FROM task_bullets WHERE task_gid = %s", (gid,)
    ).fetchone()
    if row is None:
        return None
    return {"content_hash": row["content_hash"], "bullets": _as_list(row["bullets"])}


def put_bullets(conn: Any, gid: str, content_hash: str, bullets: list[str]) -> None:
    conn.execute(
        """
        INSERT INTO task_bullets (task_gid, content_hash, bullets)
        VALUES (%s, %s, %s::jsonb)
        ON CONFLICT (task_gid) DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            bullets = EXCLUDED.bullets,
            updated_at = now()
        """,
        (gid, content_hash, json.dumps(bullets)),
    )


def get_state(conn: Any) -> dict:
    row = conn.execute("SELECT dirty_at, last_rebuilt_at FROM digest_state WHERE id").fetchone()
    if row is None:
        return {"dirty_at": None, "last_rebuilt_at": None}
    return {"dirty_at": row["dirty_at"], "last_rebuilt_at": row["last_rebuilt_at"]}


def mark_dirty(conn: Any) -> None:
    conn.execute(
        "INSERT INTO digest_state (id, dirty_at) VALUES (true, now()) "
        "ON CONFLICT (id) DO UPDATE SET dirty_at = now()"
    )


def mark_rebuilt(conn: Any) -> None:
    conn.execute(
        "INSERT INTO digest_state (id, last_rebuilt_at) VALUES (true, now()) "
        "ON CONFLICT (id) DO UPDATE SET last_rebuilt_at = now()"
    )

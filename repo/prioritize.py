"""The prioritizer's five tables. Takes an open connection; JSONB crosses the
wire as json.dumps text (both drivers accept it) and comes back parsed on
psycopg and as text on pg8000, hence _as_json."""

import json
from datetime import date, datetime
from typing import Any

from models.prioritize import Overrides, ScoredSet, Stats, TaskFacts

_FACT_COLS = (
    "task_gid, project_gid, project_name, parent_gid, name, permalink_url, priority, due_on, "
    "due_at, start_on, started_at, story_points, points_estimated, completed, completed_at, "
    "created_at, modified_at, tags, dependencies, dependents, num_open_subtasks, content_hash"
)


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    return value if isinstance(value, date) else date.fromisoformat(str(value))


# ---- task_facts -----------------------------------------------------------


def upsert_facts(conn: Any, f: TaskFacts) -> None:
    conn.execute(
        f"""
        INSERT INTO task_facts ({_FACT_COLS}, fetched_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
        ON CONFLICT (task_gid) DO UPDATE SET
            project_gid = EXCLUDED.project_gid, project_name = EXCLUDED.project_name,
            parent_gid = EXCLUDED.parent_gid, name = EXCLUDED.name,
            permalink_url = EXCLUDED.permalink_url, priority = EXCLUDED.priority,
            due_on = EXCLUDED.due_on, due_at = EXCLUDED.due_at, start_on = EXCLUDED.start_on,
            started_at = EXCLUDED.started_at, story_points = EXCLUDED.story_points,
            completed = EXCLUDED.completed, completed_at = EXCLUDED.completed_at,
            created_at = EXCLUDED.created_at, modified_at = EXCLUDED.modified_at,
            tags = EXCLUDED.tags, dependencies = EXCLUDED.dependencies,
            dependents = EXCLUDED.dependents, num_open_subtasks = EXCLUDED.num_open_subtasks,
            content_hash = EXCLUDED.content_hash, fetched_at = now()
        """,
        (
            f.gid,
            f.project_gid,
            f.project_name,
            f.parent_gid,
            f.name,
            f.permalink_url,
            f.priority,
            f.due_on,
            f.due_at,
            f.start_on,
            f.started_at,
            f.story_points,
            f.points_estimated,
            f.completed,
            f.completed_at,
            f.created_at,
            f.modified_at,
            json.dumps(list(f.tags)),
            json.dumps(list(f.dependencies)),
            json.dumps(list(f.dependents)),
            f.num_open_subtasks,
            f.content_hash,
        ),
    )
    # points_estimated is deliberately NOT in the UPDATE SET: only claim_estimate writes it.


def _row_to_facts(r: dict) -> TaskFacts:
    return TaskFacts(
        gid=r["task_gid"],
        project_gid=r["project_gid"],
        project_name=r["project_name"],
        parent_gid=r["parent_gid"],
        name=r["name"],
        permalink_url=r["permalink_url"],
        priority=r["priority"],
        due_on=_as_date(r["due_on"]),
        due_at=r["due_at"],
        start_on=_as_date(r["start_on"]),
        started_at=_as_date(r["started_at"]),
        story_points=r["story_points"],
        points_estimated=r["points_estimated"],
        completed=bool(r["completed"]),
        completed_at=r["completed_at"],
        created_at=r["created_at"],
        modified_at=r["modified_at"],
        tags=tuple(_as_json(r["tags"], [])),
        dependencies=tuple(_as_json(r["dependencies"], [])),
        dependents=tuple(_as_json(r["dependents"], [])),
        num_open_subtasks=int(r["num_open_subtasks"] or 0),
        content_hash=r["content_hash"],
    )


def get_facts(conn: Any, gid: str) -> TaskFacts | None:
    row = conn.execute(
        f"SELECT {_FACT_COLS} FROM task_facts WHERE task_gid = %s", (gid,)
    ).fetchone()
    return _row_to_facts(row) if row else None


def list_facts(conn: Any) -> list[TaskFacts]:
    return [
        _row_to_facts(r) for r in conn.execute(f"SELECT {_FACT_COLS} FROM task_facts").fetchall()
    ]


def list_facts_index(conn: Any) -> dict[str, tuple[datetime, str]]:
    rows = conn.execute("SELECT task_gid, fetched_at, content_hash FROM task_facts").fetchall()
    return {r["task_gid"]: (r["fetched_at"], r["content_hash"]) for r in rows}


def list_open_gids(conn: Any) -> set[str]:
    rows = conn.execute("SELECT task_gid FROM task_facts WHERE NOT completed").fetchall()
    return {r["task_gid"] for r in rows}


def delete_task(conn: Any, gid: str) -> None:
    for table in ("task_scores", "task_enrichment", "task_overrides", "task_facts"):
        conn.execute(f"DELETE FROM {table} WHERE task_gid = %s", (gid,))


# ---- task_enrichment -----------------------------------------------------


def get_enrichment(conn: Any, gid: str) -> tuple[str, dict] | None:
    """(content_hash, raw) for one task, or None."""
    row = conn.execute(
        "SELECT content_hash, raw FROM task_enrichment WHERE task_gid = %s", (gid,)
    ).fetchone()
    return (row["content_hash"], _as_json(row["raw"], {})) if row else None


def upsert_enrichment(conn: Any, gid: str, content_hash: str, raw: dict, model: str) -> None:
    conn.execute(
        """
        INSERT INTO task_enrichment (task_gid, content_hash, raw, model, created_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (task_gid) DO UPDATE SET
            content_hash = EXCLUDED.content_hash, raw = EXCLUDED.raw,
            model = EXCLUDED.model, created_at = now()
        """,
        (gid, content_hash, json.dumps(raw), model),
    )


def list_enrichment(conn: Any) -> dict[str, tuple[str, dict]]:
    rows = conn.execute("SELECT task_gid, content_hash, raw FROM task_enrichment").fetchall()
    return {r["task_gid"]: (r["content_hash"], _as_json(r["raw"], {})) for r in rows}


# ---- task_overrides ------------------------------------------------------

_COLUMN_OVERRIDES = ("pinned_rank", "snooze_until")


def _row_to_overrides(r: dict | None) -> Overrides:
    if not r:
        return Overrides.NONE
    return Overrides(
        fields=_as_json(r["overrides"], {}),
        pinned_rank=r["pinned_rank"],
        snooze_until=_as_date(r["snooze_until"]),
    )


def get_overrides(conn: Any, gid: str) -> Overrides:
    row = conn.execute(
        "SELECT overrides, pinned_rank, snooze_until FROM task_overrides WHERE task_gid = %s",
        (gid,),
    ).fetchone()
    return _row_to_overrides(row)


def list_overrides(conn: Any) -> dict[str, Overrides]:
    rows = conn.execute(
        "SELECT task_gid, overrides, pinned_rank, snooze_until FROM task_overrides"
    ).fetchall()
    return {r["task_gid"]: _row_to_overrides(r) for r in rows}


def merge_overrides(conn: Any, gid: str, patch: dict) -> Overrides:
    """None clears a key. Column keys go to columns; the rest merge into JSONB."""
    current = get_overrides(conn, gid)
    fields = dict(current.fields)
    pinned, snooze = current.pinned_rank, current.snooze_until
    for key, value in patch.items():
        if key == "pinned_rank":
            pinned = value
        elif key == "snooze_until":
            snooze = _as_date(value)
        elif value is None:
            fields.pop(key, None)
        else:
            fields[key] = value
    conn.execute(
        """
        INSERT INTO task_overrides (task_gid, overrides, pinned_rank, snooze_until, updated_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (task_gid) DO UPDATE SET
            overrides = EXCLUDED.overrides, pinned_rank = EXCLUDED.pinned_rank,
            snooze_until = EXCLUDED.snooze_until, updated_at = now()
        """,
        (gid, json.dumps(fields), pinned, snooze),
    )
    return Overrides(fields=fields, pinned_rank=pinned, snooze_until=snooze)


def clear_pin(conn: Any, gid: str) -> None:
    conn.execute(
        "UPDATE task_overrides SET pinned_rank = NULL, updated_at = now() WHERE task_gid = %s",
        (gid,),
    )


def claim_estimate(conn: Any, gid: str, points: int) -> bool:
    cur = conn.execute(
        "UPDATE task_facts SET points_estimated = %s WHERE task_gid = %s AND points_estimated IS NULL",
        (points, gid),
    )
    return cur.rowcount == 1


def set_story_points(conn: Any, gid: str, points: int) -> None:
    """Record points just written to Asana, so the facts row need not wait for
    an echo event — which a subtask never gets (project webhooks skip them)."""
    conn.execute(
        "UPDATE task_facts SET story_points = %s, fetched_at = now() WHERE task_gid = %s",
        (points, gid),
    )


# ---- task_scores ---------------------------------------------------------

RESCORE_LOCK_KEY = 7231


def lock_rescore(conn: Any) -> None:
    """Serialise rescores: replace_scores is DELETE-all + INSERT-all, so two
    concurrent ones collide on the primary key. Released at commit/rollback."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (RESCORE_LOCK_KEY,))


def replace_scores(conn: Any, scored: ScoredSet) -> None:
    conn.execute("DELETE FROM task_scores")
    for t in scored.tasks:
        conn.execute(
            """
            INSERT INTO task_scores
                (task_gid, scored_at, today, bucket, score, position, rank, components,
                 overcommitted, stale, stale_reason)
            VALUES (%s, now(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                t.gid,
                scored.today,
                t.bucket,
                t.score,
                t.position,
                t.rank,
                json.dumps(t.components, default=str),
                t.overcommitted,
                t.stale,
                t.stale_reason,
            ),
        )


def list_scores(conn: Any) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.task_gid, s.scored_at, s.today, s.bucket, s.score, s.position, s.rank,
               s.components, s.overcommitted, s.stale, s.stale_reason,
               f.name, f.project_name, f.permalink_url, f.due_on, f.story_points, f.started_at,
               o.pinned_rank, o.snooze_until, o.overrides
        FROM task_scores s
        JOIN task_facts f USING (task_gid)
        LEFT JOIN task_overrides o USING (task_gid)
        ORDER BY s.position
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["components"] = _as_json(d["components"], {})
        d["overrides"] = _as_json(d.get("overrides"), {})
        d["due_on"] = _as_date(d["due_on"])
        d["snooze_until"] = _as_date(d.get("snooze_until"))
        d["started_at"] = _as_date(d.get("started_at"))
        out.append(d)
    return out


# ---- prioritize_runs -----------------------------------------------------


def insert_run(
    conn: Any, *, kind: str, today: date, trigger_gid: str | None, top: list[dict]
) -> int:
    row = conn.execute(
        "INSERT INTO prioritize_runs (kind, today, trigger_gid, top) VALUES (%s, %s, %s, %s) RETURNING run_id",
        (kind, today, trigger_gid, json.dumps(top, default=str)),
    ).fetchone()
    return int(row["run_id"])


def last_daily_run(conn: Any) -> dict | None:
    row = conn.execute(
        "SELECT run_id, today, top FROM prioritize_runs WHERE kind = 'daily' ORDER BY ran_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "today": _as_date(row["today"]),
        "top": _as_json(row["top"], []),
    }


def set_run_top(conn: Any, run_id: int, top: list[dict]) -> None:
    conn.execute(
        "UPDATE prioritize_runs SET top = %s WHERE run_id = %s",
        (json.dumps(top, default=str), run_id),
    )


# ---- task_stats ----------------------------------------------------------


def list_stats(conn: Any) -> dict[str, Stats]:
    rows = conn.execute("SELECT task_gid, times_deferred FROM task_stats").fetchall()
    return {r["task_gid"]: Stats(times_deferred=int(r["times_deferred"] or 0)) for r in rows}


def bump_deferred(conn: Any, gids: list[str], today: date) -> None:
    for gid in gids:
        conn.execute(
            """
            INSERT INTO task_stats (task_gid, times_deferred, last_offered)
            VALUES (%s, 1, %s)
            ON CONFLICT (task_gid) DO UPDATE SET
                times_deferred = task_stats.times_deferred + 1, last_offered = EXCLUDED.last_offered
            """,
            (gid, today),
        )


def snapshot_completion(conn: Any, f: TaskFacts) -> None:
    cycle = None
    if f.started_at and f.completed_at:
        cycle = round((f.completed_at.date() - f.started_at).days + f.completed_at.hour / 24, 2)
    conn.execute(
        """
        INSERT INTO task_stats
            (task_gid, project_name, started_at, completed_at, points_at_completion,
             points_estimated, cycle_days)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (task_gid) DO UPDATE SET
            project_name = EXCLUDED.project_name, started_at = EXCLUDED.started_at,
            completed_at = EXCLUDED.completed_at,
            points_at_completion = EXCLUDED.points_at_completion,
            points_estimated = EXCLUDED.points_estimated, cycle_days = EXCLUDED.cycle_days
        """,
        (
            f.gid,
            f.project_name,
            f.started_at,
            f.completed_at,
            f.story_points,
            f.points_estimated,
            cycle,
        ),
    )


def calibration_rows(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT project_name, points_at_completion, points_estimated, cycle_days, times_deferred
        FROM task_stats
        """
    ).fetchall()

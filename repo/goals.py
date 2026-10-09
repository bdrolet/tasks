"""goal_state, goal_reports, goal_overrides, strategy_snapshot — the strategy
layer's tables. Takes an open connection."""

import json
from datetime import date
from typing import Any

from models.strategy import Goal, GoalState, Strategy


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def upsert_state(conn: Any, state: GoalState) -> None:
    conn.execute(
        """
        INSERT INTO goal_state (goal_id, day, kind, strategy_hash, state)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (goal_id, day) DO UPDATE SET
            kind = EXCLUDED.kind, strategy_hash = EXCLUDED.strategy_hash, state = EXCLUDED.state
        """,
        (state.goal_id, state.day, state.kind, state.strategy_hash, json.dumps(state.state)),
    )


def get_states(conn: Any, day: date) -> dict[str, GoalState]:
    rows = conn.execute(
        "SELECT goal_id, day, kind, strategy_hash, state FROM goal_state WHERE day = %s", (day,)
    ).fetchall()
    return {
        r["goal_id"]: GoalState(r["goal_id"], r["kind"], r["day"], r["strategy_hash"], _as_json(r["state"], {}))
        for r in rows
    }


def set_next_steps(conn: Any, day: date, steps: dict[str, str | None]) -> None:
    for goal_id, gid in steps.items():
        conn.execute(
            """
            UPDATE goal_state SET state = state || %s::jsonb WHERE goal_id = %s AND day = %s
            """,
            (json.dumps({"next_step": gid, "stalled": gid is None}), goal_id, day),
        )


def insert_report(conn: Any, goal_id: str, value: float, period_start: date | None) -> int:
    row = conn.execute(
        "INSERT INTO goal_reports (goal_id, value, period_start) VALUES (%s, %s, %s) RETURNING id",
        (goal_id, value, period_start),
    ).fetchone()
    return int(row["id"])


def latest_reports(conn: Any, goal_id: str, limit: int = 3) -> list[dict]:
    return conn.execute(
        "SELECT value, period_start, reported_at FROM goal_reports WHERE goal_id = %s "
        "ORDER BY reported_at DESC LIMIT %s",
        (goal_id, limit),
    ).fetchall()


def all_latest_reports(conn: Any, limit: int = 3) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT goal_id, value, period_start, reported_at FROM goal_reports ORDER BY reported_at DESC"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["goal_id"], [])
        if len(bucket) < limit:
            bucket.append(r)
    return out


def save_snapshot(conn: Any, strategy: Strategy) -> None:
    goals = [
        {"id": g.id, "kind": g.kind, "weight": g.weight, "horizon": g.horizon.isoformat() if g.horizon else None}
        for g in strategy.goals
    ]
    conn.execute(
        """
        INSERT INTO strategy_snapshot (id, text_hash, last_reviewed, findings, goals, updated_at)
        VALUES (1, %s, %s, %s, %s, now())
        ON CONFLICT (id) DO UPDATE SET text_hash = EXCLUDED.text_hash,
            last_reviewed = EXCLUDED.last_reviewed, findings = EXCLUDED.findings,
            goals = EXCLUDED.goals, updated_at = now()
        """,
        (strategy.text_hash, strategy.last_reviewed, json.dumps(list(strategy.findings)), json.dumps(goals)),
    )


def load_snapshot(conn: Any) -> Strategy:
    row = conn.execute(
        "SELECT text_hash, last_reviewed, findings, goals FROM strategy_snapshot WHERE id = 1"
    ).fetchone()
    if row is None:
        return Strategy.EMPTY
    goals = tuple(
        Goal(id=g["id"], kind=g["kind"], weight=float(g.get("weight") or 1.0),
             horizon=date.fromisoformat(g["horizon"]) if g.get("horizon") else None)
        for g in _as_json(row["goals"], [])
    )
    lr = row["last_reviewed"]
    return Strategy(goals=goals, last_reviewed=lr if isinstance(lr, date) or lr is None else date.fromisoformat(str(lr)),
                    findings=tuple(_as_json(row["findings"], [])), text_hash=row["text_hash"])


def get_mutes(conn: Any) -> dict[str, date]:
    rows = conn.execute("SELECT goal_id, mute_until FROM goal_overrides WHERE mute_until IS NOT NULL").fetchall()
    return {r["goal_id"]: r["mute_until"] for r in rows}


def set_mute(conn: Any, goal_id: str, until: date | None) -> None:
    conn.execute(
        """
        INSERT INTO goal_overrides (goal_id, mute_until) VALUES (%s, %s)
        ON CONFLICT (goal_id) DO UPDATE SET mute_until = EXCLUDED.mute_until, updated_at = now()
        """,
        (goal_id, until),
    )

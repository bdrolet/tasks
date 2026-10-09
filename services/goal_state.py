# services/goal_state.py
"""Daily evaluation of each goal and area: lead rates, the latest lag,
tripwires, below-the-line signals with debounce, and the lead/lag
diagnosis. Pure — the handler supplies views built from task_facts and
yesterday's task_scores. Design: strategy-layer spec D7, D9, D11."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models.prioritize import TaskFacts
from models.strategy import PERIOD_DAYS, Goal, GoalState, Signal, Strategy
from services.due_digest import LOCAL_TZ
from services.prioritize_config import Config

PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
LEAD_HISTORY_DAYS = 90
ACTIONABLE = "next"


@dataclass(frozen=True)
class TaskView:
    gid: str
    serves: tuple[str, ...]
    role: str | None
    bucket: str
    priority: str | None
    due_on: date | None
    completed: bool
    completed_at: datetime | None
    tags: tuple[str, ...]


def views_from(facts: list[TaskFacts], scores: dict[str, dict]) -> list[TaskView]:
    out = []
    for f in facts:
        c = (scores.get(f.gid) or {}).get("components") or {}
        bucket = (scores.get(f.gid) or {}).get("bucket") or (
            "excluded:completed" if f.completed else ACTIONABLE
        )
        serves = tuple(c.get("serves") or ())
        if not serves:  # a completed task has no fresh score row; fall back to its tags
            serves = tuple(
                t.partition(":")[2] for t in f.tags if t.casefold().startswith("serves:")
            )
        out.append(
            TaskView(
                f.gid,
                serves,
                c.get("role"),
                bucket,
                f.priority,
                f.due_on,
                f.completed,
                f.completed_at,
                f.tags,
            )
        )
    return out


def compare(op: str, value: float, threshold: float) -> bool:
    return {
        ">=": value >= threshold,
        "<=": value <= threshold,
        "=": value == threshold,
        "<": value < threshold,
        ">": value > threshold,
    }[op]


def window_days(period: str) -> int:
    return PERIOD_DAYS[period]


def _local_date(ts: datetime) -> date:
    return ts.astimezone(ZoneInfo(LOCAL_TZ)).date()


def _completed_within(
    views: list[TaskView],
    goal_id: str,
    tag: str | None,
    days: int,
    today: date,
    inclusive: bool = False,
) -> list[TaskView]:
    start = today - timedelta(days=days)
    return [
        v
        for v in views
        if v.completed
        and v.completed_at
        and (
            _local_date(v.completed_at) >= start
            if inclusive
            else _local_date(v.completed_at) > start
        )
        and goal_id in v.serves
        and (tag is None or tag in v.tags)
    ]


def _count_since(
    views: list[TaskView], goal_id: str, tag: str, since: date | None, today: date
) -> int:
    days = (today - since).days if since else 10_000
    return len(_completed_within(views, goal_id, tag, days, today))


def _signal(
    sig: Signal, goal: Goal, views: list[TaskView], today: date, tagged: int, config: Config
) -> tuple[bool | None, list[str]]:
    if tagged < config.min_tagged_for_signals:
        return None, []
    mine = [v for v in views if goal.id in v.serves]
    open_actionable = [v for v in mine if not v.completed and v.bucket == ACTIONABLE]
    if sig.kind == "overdue":
        hits = [
            v
            for v in open_actionable
            if v.role in ("path", "derisk")
            and v.due_on is not None
            and (today - v.due_on).days > sig.grace
            and (
                sig.tag in v.tags
                if sig.tag
                else PRIORITY_RANK.get(v.priority or "P2", 2) <= PRIORITY_RANK[sig.min_priority]
            )
        ]
        return bool(hits), [v.gid for v in hits]
    if sig.kind == "undated":
        if sig.after is None or today <= sig.after:
            return False, []
        hits = [v for v in open_actionable if sig.tag in v.tags and v.due_on is None]
        return bool(hits), [v.gid for v in hits]
    if sig.kind == "stale":
        if not open_actionable:
            return None, []
        recent = _completed_within(views, goal.id, None, sig.days or 0, today, inclusive=True)
        return not recent, []
    if sig.kind == "lead":
        history = _completed_within(views, goal.id, sig.tag, LEAD_HISTORY_DAYS, today)
        if not history:
            return None, []
        count = len(
            _completed_within(views, goal.id, sig.tag, window_days(sig.period or "week"), today)
        )
        return count < (sig.value or 0), []
    return None, []


def _debounce(prev: dict | None, raw: bool | None, config: Config) -> tuple[bool, int]:
    """(effective, consecutive_days). A run counts days the raw value has
    held its current truth; effective flips only when the run reaches
    signal_debounce_days (D9)."""
    if raw is None:
        return False, 0
    p_raw = prev.get("raw") if prev else None
    p_eff = bool(prev.get("effective")) if prev else False
    p_run = int(prev.get("consecutive_days") or 0) if prev else 0
    run = p_run + 1 if p_raw is raw else 1
    if raw and not p_eff:
        return run >= config.signal_debounce_days, run
    if not raw and p_eff:
        return not (run >= config.signal_debounce_days), run
    return p_eff, run


def _area_state(
    goal: Goal,
    views: list[TaskView],
    mutes: dict[str, date],
    prev: GoalState | None,
    today: date,
    config: Config,
) -> dict:
    tagged = sum(1 for v in views if goal.id in v.serves)
    if prev is not None and prev.day != today - timedelta(days=1):
        prev = None  # debounce counts consecutive daily evaluations only
    prev_signals = {s["signal"]: s for s in (prev.state.get("signals") if prev else []) or []}
    signals = []
    for sig in goal.signals:
        raw, tasks = _signal(sig, goal, views, today, tagged, config)
        effective, run = _debounce(prev_signals.get(sig.text), raw, config)
        signals.append(
            {
                "signal": sig.text,
                "class": sig.cls,
                "raw": raw,
                "effective": effective,
                "consecutive_days": run,
                "state": "no_data" if raw is None else ("true" if effective else "false"),
                "tasks": tasks,
            }
        )
    muted_until = mutes.get(goal.id)
    muted = muted_until is not None and muted_until >= today
    return {
        "signals": signals,
        "below_the_line": (not muted) and any(s["effective"] for s in signals),
        "evidence_below_the_line": (not muted)
        and any(s["effective"] and s["class"] == "evidence" for s in signals),
        "muted_until": muted_until.isoformat() if muted else None,
        "tagged": tagged,
    }


def _outcome_state(
    goal: Goal,
    views: list[TaskView],
    reports: list[dict],
    last_reviewed: date | None,
    today: date,
    config: Config,
) -> dict:
    leads = []
    for m in goal.leads:
        count = len(_completed_within(views, goal.id, m.tag, window_days(m.period), today))
        leads.append(
            {
                "tag": m.tag,
                "window": m.period,
                "value": count,
                "threshold": m.value,
                "met": compare(m.op, count, m.value),
            }
        )
    lag = None
    if goal.lag and reports:
        latest = reports[0]
        ps = latest.get("period_start")
        lag = {
            "value": float(latest["value"]),
            "threshold": goal.lag.value,
            "met": compare(goal.lag.op, float(latest["value"]), goal.lag.value),
            "period_start": ps.isoformat() if isinstance(ps, date) else ps,
        }
    tripwires = []
    for t in goal.tripwires:
        evaluated = today >= t.by
        if t.subject == "lag":
            value = float(reports[0]["value"]) if reports else None
        else:
            value = _count_since(views, goal.id, t.subject, last_reviewed, today)
        fired = bool(evaluated and value is not None and compare(t.op, value, t.value))
        tripwires.append(
            {
                "ordinal": t.ordinal,
                "text": f"{t.subject} {t.op} {t.value:g} by {t.by.isoformat()}",
                "action": t.action,
                "by": t.by.isoformat(),
                "value": value,
                "evaluated": evaluated,
                "fired": fired,
            }
        )
    leads_met = bool(leads) and all(x["met"] for x in leads)
    if goal.lag is None or len(reports) < config.lag_flat_periods:
        diagnosis = "insufficient data"
    elif not leads_met:
        diagnosis = "lead weak"
    elif all(
        not compare(goal.lag.op, float(r["value"]), goal.lag.value)
        for r in reports[: config.lag_flat_periods]
    ):
        diagnosis = "lead strong, lag flat"
    else:
        diagnosis = "on track"
    return {
        "leads": leads,
        "lag": lag,
        "tripwires": tripwires,
        "diagnosis": diagnosis,
        "horizon": goal.horizon.isoformat() if goal.horizon else None,
    }


def evaluate(
    strategy: Strategy,
    views: list[TaskView],
    reports: dict[str, list[dict]],
    mutes: dict[str, date],
    previous: dict[str, GoalState],
    today: date,
    config: Config,
) -> list[GoalState]:
    out = []
    for g in strategy.goals:
        if g.kind == "area":
            state = _area_state(g, views, mutes, previous.get(g.id), today, config)
        else:
            state = _outcome_state(
                g, views, reports.get(g.id, []), strategy.last_reviewed, today, config
            )
        state.setdefault("next_step", None)
        state.setdefault("stalled", True)
        out.append(GoalState(g.id, g.kind, today, strategy.text_hash, state))
    return out

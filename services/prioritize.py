"""The "do next" scorer — pure: no I/O, no clock, no config file read.
handlers/prioritize.py feeds it facts from the DB; api/routers/next.py reruns
select() from stored components. Every constant comes from Config.
Design: docs/superpowers/specs/2026-09-23-next-prioritizer-design.md"""

import math
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models.prioritize import Enrichment, Overrides, ScoredSet, ScoredTask, Stats, TaskFacts
from services.due_digest import LOCAL_TZ
from services.prioritize_config import Config

_PRIORITY_RE = re.compile(r"^\[P([0-3])\]")
_TAG_FIELDS = ("waiting", "energy", "impact")
IMPACTS = ("low", "medium", "high")
ENERGIES = ("deep", "shallow")


def parse_priority(name: str) -> str | None:
    m = _PRIORITY_RE.match(name or "")
    return f"P{m.group(1)}" if m else None


@dataclass(frozen=True)
class Effective:
    points: int
    points_source: str  # field | estimate | default
    points_confidence: str
    waiting_on: str | None
    due_date_inferred: date | None
    due_date_inferred_confidence: str
    impact: str
    energy: str


def _tag_values(tags: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tag in tags:
        key, sep, value = tag.partition(":")
        key, value = key.strip().casefold(), value.strip()
        if sep and key in _TAG_FIELDS and value:
            out[key] = value
    return out


def effective(
    facts: TaskFacts, enrichment: Enrichment, overrides: Overrides, config: Config
) -> Effective:
    """tag > override > model > default, per field."""
    tags = _tag_values(facts.tags)
    o = overrides.fields

    def pick(tag_key: str, field: str, model_value, valid=None):
        for value in (tags.get(tag_key), o.get(field)):
            if value is not None and (valid is None or value in valid):
                return value
        return model_value

    if facts.story_points is not None:
        points, source, conf = facts.story_points, "field", "high"
    elif o.get("story_points") is not None:
        points, source, conf = int(o["story_points"]), "field", "high"
    elif facts.points_estimated is not None:
        points, source, conf = facts.points_estimated, "estimate", enrichment.points_confidence
    elif enrichment.story_points_suggested is not None:
        points, source, conf = (
            enrichment.story_points_suggested,
            "estimate",
            enrichment.points_confidence,
        )
    else:
        points, source, conf = config.default_points, "default", "low"

    inferred = o.get("due_date_inferred", enrichment.due_date_inferred)
    if isinstance(inferred, str):
        try:
            inferred = date.fromisoformat(inferred)
        except ValueError:
            inferred = None
    inferred_conf = (
        "high"
        if "due_date_inferred" in o and o["due_date_inferred"]
        else enrichment.due_date_inferred_confidence
    )
    return Effective(
        points=points,
        points_source=source,
        points_confidence=conf,
        waiting_on=pick("waiting", "waiting_on", enrichment.waiting_on),
        due_date_inferred=inferred,
        due_date_inferred_confidence=inferred_conf,
        impact=pick("impact", "impact", enrichment.impact, IMPACTS),
        energy=pick("energy", "energy", enrichment.energy, ENERGIES),
    )


def _local_date(ts: datetime) -> date:
    return ts.astimezone(ZoneInfo(LOCAL_TZ)).date()


def _effective_due(facts: TaskFacts, eff: Effective, config: Config) -> tuple[date | None, str]:
    """(date, source): hard due_on; else a medium/high-confidence inferred
    date; else the horizon created_at + horizon[priority]; else none."""
    if facts.due_on:
        return facts.due_on, "hard"
    if eff.due_date_inferred and eff.due_date_inferred_confidence in ("medium", "high"):
        return eff.due_date_inferred, "inferred"
    horizon = config.horizon_days.get(facts.priority or config.default_priority)
    if horizon is None:
        return None, "none"
    return _local_date(facts.created_at) + timedelta(days=horizon), "horizon"


def _bucket(
    facts: TaskFacts,
    eff: Effective,
    ov: Overrides,
    open_gids: set[str],
    today: date,
    config: Config,
) -> tuple[str, str | None]:
    """(bucket, pinned_despite). Order: completed, snoozed, excluded project,
    blocked, parent, waiting. A pin overrides only the last three (D15)."""
    if facts.completed:
        return "excluded:completed", None
    if ov.snooze_until and ov.snooze_until > today:
        return "snoozed", None
    if facts.project_name in config.excluded_projects:
        return "excluded:project", None
    reason = None
    if any(d in open_gids for d in facts.dependencies):
        reason = "blocked"
    elif facts.num_open_subtasks > 0:
        reason = "parent"
    elif eff.waiting_on:
        reason = "waiting"
    if reason is None:
        return "next", None
    if ov.pinned_rank is not None:
        return "next", reason
    return ("nudge" if reason == "waiting" else f"excluded:{reason}"), None


def score_set(
    facts: list[TaskFacts],
    enrichments: dict[str, Enrichment],
    overrides: dict[str, Overrides],
    stats: dict[str, Stats],
    config: Config,
    today: date,
    project_last_offered: dict[str, date] | None = None,
) -> ScoredSet:
    """`project_last_offered` maps project name -> the last day one of its
    tasks was in a daily pick; a project absent from it has never been
    offered and gets the full starvation boost (D14)."""
    last_offered = project_last_offered or {}
    open_gids = {f.gid for f in facts if not f.completed}
    # A parent's stored num_open_subtasks is only refreshed when the parent
    # itself is gathered; a subtask completing moves only the subtask's row.
    # Where any subtask row exists for a parent, the rows are the truth.
    children_seen: set[str] = set()
    open_children: dict[str, int] = {}
    for f in facts:
        if f.parent_gid:
            children_seen.add(f.parent_gid)
            if not f.completed:
                open_children[f.parent_gid] = open_children.get(f.parent_gid, 0) + 1
    tasks: list[ScoredTask] = []
    pending: list[tuple[TaskFacts, Effective, ScoredTask, date | None, str]] = []

    for f in facts:
        if f.gid in children_seen:
            f = replace(f, num_open_subtasks=open_children.get(f.gid, 0))
        e = enrichments.get(f.gid, Enrichment.DEFAULT)
        ov = overrides.get(f.gid, Overrides.NONE)
        eff = effective(f, e, ov, config)
        bucket, despite = _bucket(f, eff, ov, open_gids, today, config)
        due, source = _effective_due(f, eff, config)
        effort = eff.points / config.points_per_day
        if eff.points_source != "field" and eff.points_confidence == "low":
            effort *= config.low_confidence_multiplier
        days_stale = max(0, (today - _local_date(f.modified_at)).days)
        offered = last_offered.get(f.project_name or "")
        days_offered = (today - offered).days if offered is not None else None
        boost = (
            config.starvation_max_boost
            if days_offered is None
            else min(
                config.starvation_max_boost,
                config.starvation_boost_per_day * max(0, days_offered),
            )
        )
        t = ScoredTask(
            gid=f.gid,
            bucket=bucket,
            score=None,
            position=0,
            rank=None,
            components={
                "priority": f.priority or config.default_priority,
                "points": eff.points,
                "points_source": eff.points_source,
                "points_confidence": eff.points_confidence,
                "effort_days": effort,
                "effective_due": due.isoformat() if due else None,
                "due_source": source,
                "soft": source != "hard",
                "days_until_due": (due - today).days if due else None,
                "days_stale": days_stale,
                "impact": eff.impact,
                "energy": eff.energy,
                "waiting_on": eff.waiting_on,
                "unenriched": e.unenriched,
                "reason": e.reason,
                "override": {
                    "pinned_rank": ov.pinned_rank,
                    "snooze_until": ov.snooze_until.isoformat() if ov.snooze_until else None,
                    "fields": sorted(ov.fields),
                },
                "pinned_despite": despite,
                "starvation_boost": boost,
                "days_since_project_offered": days_offered,
            },
            project_name=f.project_name,
            points=eff.points,
            energy=eff.energy,
            pinned_rank=ov.pinned_rank,
        )
        tasks.append(t)
        pending.append((f, eff, t, due, source))

    # Feasibility (D13): earliest-deadline-first over actionable HARD dates
    # only. A horizon or inferred date is a nudge toward urgency, not a
    # commitment; queueing them made nearly everything "overcommitted".
    for f, eff, t, due, source in pending:
        c = t.components
        c["simulated_start"] = None
        c["slack"] = c["effective_slack"] = (
            c["days_until_due"] - c["effort_days"] if due is not None else None
        )
    hard = [p for p in pending if p[2].bucket == "next" and p[4] == "hard"]
    hard.sort(key=lambda p: p[3] or date.max)
    cursor = 0.0
    for f, eff, t, due, source in hard:
        c = t.components
        c["simulated_start"] = cursor
        c["effective_slack"] = c["days_until_due"] - (cursor + c["effort_days"])
        t.overcommitted = c["effective_slack"] < 0
        cursor += c["effort_days"]

    caps = {"inferred": config.soft_cap_inferred, "horizon": config.soft_cap_horizon}
    # Score every non-completed task so nudge/excluded rows are explainable too.
    for f, eff, t, due, source in pending:
        if t.bucket == "excluded:completed":
            continue
        c = t.components
        p_weight = config.priority_weight.get(
            c["priority"], config.priority_weight[config.default_priority]
        )
        if due is None:
            u = config.no_due_urgency
        else:
            # effective_slack is the EDF result for hard dates in the pass and
            # raw slack for everything else.
            eslack = c["effective_slack"]
            u = 1.0 / (1.0 + math.exp(config.urgency_k * (eslack - config.urgency_s0)))
            if source in caps:
                u = min(u, caps[source])
        a = min(1.0, c["days_stale"] / config.stale_days)
        cat = config.category_weight.get(f.project_name or "", config.default_category_weight)
        i = config.impact_weight[eff.impact]
        open_dependents = sum(1 for d in f.dependents if d in open_gids)
        b = min(1.0, config.unblock_per_task * open_dependents)
        w = config.weights
        cod = (
            w["priority"] * p_weight
            + w["urgency"] * u
            + w["impact"] * i
            + w["unblock"] * b
            + w["aging"] * a
            + w["category"] * cat
        )
        c.update({"P": p_weight, "U": u, "A": a, "C": cat, "I": i, "B": b, "cost_of_delay": cod})
        t.score = cod / max(c["effort_days"], config.min_effort_days)

        if t.bucket in ("next", "nudge"):
            deferred = stats.get(f.gid, Stats.NONE).times_deferred
            if c["priority"] in ("P2", "P3") and c["days_stale"] > config.stale_after_days:
                t.stale, t.stale_reason = True, "aged"
            elif deferred >= config.deferred_limit:
                t.stale, t.stale_reason = True, "deferred"
            elif source == "inferred" and due is not None and due < today:
                t.stale, t.stale_reason = True, "soft_due_passed"

    # Positions: the next list with pins at their pinned_rank (the same
    # placement select() uses), then everything else by score. Ranks: the
    # default selection.
    nxt = [t for t in tasks if t.bucket == "next"]
    rest = [t for t in tasks if t.bucket != "next"]
    unpinned = sorted((t for t in nxt if t.pinned_rank is None), key=lambda t: -(t.score or 0))
    ordered_next = _place_pins(unpinned, _sorted_pins(nxt))
    rest.sort(key=lambda t: -(t.score or 0))
    for pos, t in enumerate([*ordered_next, *rest], start=1):
        t.position = pos
    for rank, t in enumerate(select(ordered_next, config), start=1):
        t.rank = rank
    return ScoredSet(today=today, tasks=ordered_next + rest)


def select(
    candidates: list[ScoredTask], config: Config, *, n: int | None = None, energy: str | None = None
) -> list[ScoredTask]:
    """Must-dos first, then a greedy fill; pins inserted at their rank
    afterwards and count toward neither n nor capacity (P5).

    Must-dos are hard-dated tasks due within hard_due_window_days (overdue
    included), by score: placed whatever n, capacity, energy or diversity
    say, but they consume capacity and count as a pick of their project.
    The fill ranks by score * (1 + starvation_boost) * energy factor, with
    the same-project diversity haircut after every pick, and stops at n
    total picks or a full day. Reads only ScoredTask fields and components,
    so api/routers/next.py re-selects stored rows identically."""
    n = n or config.default_n
    pool = {t.gid: t for t in candidates if t.pinned_rank is None and t.score is not None}
    must = sorted((t for t in pool.values() if _is_must(t, config)), key=lambda t: -(t.score or 0))
    adjusted = {
        gid: (t.score or 0)
        * (1.0 + float(t.components.get("starvation_boost") or 0.0))
        * (config.energy_penalty if energy and t.energy != energy else 1.0)
        for gid, t in pool.items()
    }
    picked: list[ScoredTask] = []
    used = 0.0

    def take(t: ScoredTask) -> None:
        nonlocal used
        del pool[t.gid]
        picked.append(t)
        used += t.points
        for g, other in pool.items():
            if other.project_name == t.project_name:
                adjusted[g] *= config.diversity_penalty

    for t in must:
        take(t)
    while pool and len(picked) < n and used < config.points_per_day:
        take(pool[max(pool, key=lambda g: adjusted[g])])
    return _place_pins(picked, _sorted_pins(candidates))


def _is_must(t: ScoredTask, config: Config) -> bool:
    c = t.components
    days = c.get("days_until_due")
    return (
        c.get("due_source") == "hard" and days is not None and days <= config.hard_due_window_days
    )


def _sorted_pins(tasks: list[ScoredTask]) -> list[ScoredTask]:
    """Pinned tasks by pinned_rank; two pins on one position keep score order."""
    return sorted(
        (t for t in tasks if t.pinned_rank is not None),
        key=lambda t: (t.pinned_rank, -(t.score or 0)),
    )


def _place_pins(
    unpinned_sorted: list[ScoredTask], pinned_sorted: list[ScoredTask]
) -> list[ScoredTask]:
    """Insert each pin at index pinned_rank - 1 (clamped to the list end);
    same-position pins stack in the order given."""
    out = list(unpinned_sorted)
    inserted_at: dict[int, int] = {}
    for t in pinned_sorted:
        base = max((t.pinned_rank or 1) - 1, 0)
        idx = min(base + inserted_at.get(base, 0), len(out))
        out.insert(idx, t)
        inserted_at[base] = inserted_at.get(base, 0) + 1
    return out


def side_lists(scored: ScoredSet) -> dict[str, list[ScoredTask]]:
    active = [t for t in scored.tasks if t.bucket in ("next", "nudge")]
    return {
        "overcommitted": [t for t in active if t.overcommitted],
        "stale": [t for t in active if t.stale],
        "nudge": sorted(
            (t for t in active if t.bucket == "nudge"), key=lambda t: -t.components["days_stale"]
        ),
    }

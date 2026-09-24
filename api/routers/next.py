"""Read side of the prioritizer: the ranking, today's selection, calibration
and manual overrides — all from task_scores; never Asana, never the model."""

import statistics
from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

import clients.pubsub as pubsub
from clients.db import get_conn
from models.prioritize import ScoredSet, ScoredTask
from repo import prioritize as repo
from services import prioritize as pz
from services import prioritize_config

router = APIRouter()

BUCKETS = ("next", "nudge", "snoozed", "excluded")
LISTS = ("overcommitted", "stale", "nudge")
OVERRIDE_FIELDS = {
    "waiting_on",
    "impact",
    "energy",
    "due_date_inferred",
    "story_points",
    "pinned_rank",
    "snooze_until",
}


class RankedTask(BaseModel):
    task_gid: str
    position: int
    rank: int | None = None
    name: str
    project: str | None = None
    permalink_url: str | None = None
    bucket: str
    score: float | None = None
    points: int | None = None
    points_source: str | None = None
    due_on: str | None = None
    effective_due: str | None = None
    soft: bool = False
    overcommitted: bool = False
    stale: bool = False
    stale_reason: str | None = None
    waiting_on: str | None = None
    summary: str | None = None
    override: dict = {}
    components: dict | None = None
    reason: str | None = None


class RankingResponse(BaseModel):
    today: str | None
    scored_at: str | None
    total: int
    tasks: list[RankedTask]


class NextRequest(BaseModel):
    energy: str | None = None
    n: int | None = Field(default=None, ge=1, le=50)
    explain: bool = False


class NextResponse(BaseModel):
    today: str | None
    scored_at: str | None
    run_id: int | None
    next: list[RankedTask]
    overcommitted: list[RankedTask]
    stale: list[RankedTask]
    nudge: list[RankedTask]
    unenriched: int


class OverridesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waiting_on: str | None = None
    impact: str | None = None
    energy: str | None = None
    due_date_inferred: str | None = None
    story_points: int | None = None
    pinned_rank: int | None = Field(default=None, ge=1)
    snooze_until: str | None = None


def _iso(value) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)


def _to_ranked(r: dict, *, explain: bool) -> RankedTask:
    c = r["components"]
    return RankedTask(
        task_gid=r["task_gid"],
        position=r["position"],
        rank=r["rank"],
        name=r["name"],
        project=r["project_name"],
        permalink_url=r["permalink_url"],
        bucket=r["bucket"],
        score=r["score"],
        points=c.get("points"),
        points_source=c.get("points_source"),
        due_on=_iso(r["due_on"]),
        effective_due=c.get("effective_due"),
        soft=bool(c.get("soft")),
        overcommitted=bool(r["overcommitted"]),
        stale=bool(r["stale"]),
        stale_reason=r["stale_reason"],
        waiting_on=c.get("waiting_on"),
        summary=None,
        override=c.get("override") or {},
        components=c if explain else None,
        reason=c.get("reason") if explain else None,
    )


def _to_scored(r: dict) -> ScoredTask:
    c = r["components"]
    return ScoredTask(
        gid=r["task_gid"],
        bucket=r["bucket"],
        score=r["score"],
        position=r["position"],
        rank=r["rank"],
        components=c,
        overcommitted=bool(r["overcommitted"]),
        stale=bool(r["stale"]),
        stale_reason=r["stale_reason"],
        project_name=r["project_name"],
        points=int(c.get("points") or 0),
        energy=c.get("energy") or "shallow",
        pinned_rank=r.get("pinned_rank"),
    )


def _rows() -> list[dict]:
    with get_conn() as conn:
        return repo.list_scores(conn)


def _stamp(rows: list[dict]) -> tuple[str | None, str | None]:
    if not rows:
        return None, None
    return _iso(rows[0]["today"]), _iso(rows[0]["scored_at"])


@router.get("/ranking", response_model=RankingResponse)
def ranking(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    bucket: str | None = None,
    list_: str | None = Query(default=None, alias="list"),
    explain: bool = False,
) -> RankingResponse:
    if bucket and list_:
        raise HTTPException(status_code=400, detail="bucket and list are mutually exclusive")
    if bucket and bucket not in BUCKETS:
        raise HTTPException(
            status_code=400, detail={"error": f"unknown bucket: {bucket}", "known": BUCKETS}
        )
    if list_ and list_ not in LISTS:
        raise HTTPException(
            status_code=400, detail={"error": f"unknown list: {list_}", "known": LISTS}
        )
    rows = _rows()
    today, scored_at = _stamp(rows)
    if list_:
        sides = pz.side_lists(ScoredSet(today=date.today(), tasks=[_to_scored(r) for r in rows]))
        keep = [t.gid for t in sides[list_]]
        by_gid = {r["task_gid"]: r for r in rows}
        chosen = [by_gid[g] for g in keep]
    else:
        want = bucket or "next"
        chosen = [
            r
            for r in rows
            if (r["bucket"].startswith("excluded:") if want == "excluded" else r["bucket"] == want)
        ]
    page = chosen[offset : offset + limit]
    return RankingResponse(
        today=today,
        scored_at=scored_at,
        total=len(chosen),
        tasks=[_to_ranked(r, explain=explain) for r in page],
    )


@router.post("/next", response_model=NextResponse)
def next_today(body: NextRequest) -> NextResponse:
    if body.energy and body.energy not in pz.ENERGIES:
        raise HTTPException(status_code=400, detail="energy must be deep or shallow")
    rows = _rows()
    today, scored_at = _stamp(rows)
    scored = ScoredSet(today=date.today(), tasks=[_to_scored(r) for r in rows])
    config = prioritize_config.load()
    picked = pz.select(scored.next(), config, n=body.n, energy=body.energy)
    sides = pz.side_lists(scored)
    by_gid = {r["task_gid"]: r for r in rows}

    def rows_for(tasks: list[ScoredTask]) -> list[RankedTask]:
        return [_to_ranked(by_gid[t.gid], explain=body.explain) for t in tasks]

    top = [
        {"gid": t.gid, "rank": i, "score": t.score, "components": t.components, "started": None}
        for i, t in enumerate(picked, 1)
    ]
    with get_conn() as conn:
        run_id = repo.insert_run(
            conn,
            kind="manual",
            today=date.fromisoformat(today) if today else date.today(),
            trigger_gid=None,
            top=top,
        )
    return NextResponse(
        today=today,
        scored_at=scored_at,
        run_id=run_id,
        next=rows_for(picked),
        overcommitted=rows_for(sides["overcommitted"]),
        stale=rows_for(sides["stale"]),
        nudge=rows_for(sides["nudge"]),
        unenriched=sum(1 for r in rows if r["components"].get("unenriched")),
    )


@router.put("/tasks/{gid}/overrides")
def put_overrides(gid: str, body: OverridesRequest) -> dict:
    patch = {k: getattr(body, k) for k in body.model_fields_set}
    if body.impact is not None and body.impact not in pz.IMPACTS:
        raise HTTPException(status_code=400, detail="impact must be low, medium or high")
    if body.energy is not None and body.energy not in pz.ENERGIES:
        raise HTTPException(status_code=400, detail="energy must be deep or shallow")
    with get_conn() as conn:
        out = repo.merge_overrides(conn, gid, patch)
    pubsub.publish_task_changed(gid, "api")
    return {
        "task_gid": gid,
        "overrides": out.fields,
        "pinned_rank": out.pinned_rank,
        "snooze_until": _iso(out.snooze_until),
    }


@router.get("/calibrate")
def calibrate() -> dict:
    with get_conn() as conn:
        rows = repo.calibration_rows(conn)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["project_name"] or "—", []).append(r)

    def summarise(rs: list[dict]) -> dict:
        per_point = [
            r["cycle_days"] / r["points_at_completion"]
            for r in rs
            if r["cycle_days"] and r["points_at_completion"]
        ]
        ratios = [
            r["points_at_completion"] / r["points_estimated"]
            for r in rs
            if r["points_at_completion"] and r["points_estimated"]
        ]
        hist: dict[str, int] = {}
        for r in rs:
            key = str(r["times_deferred"] or 0)
            hist[key] = hist.get(key, 0) + 1
        return {
            "completed": sum(1 for r in rs if r["points_at_completion"]),
            "mean_cycle_days_per_point": statistics.fmean(per_point) if per_point else None,
            "median_cycle_days_per_point": statistics.median(per_point) if per_point else None,
            "mean_points_ratio": statistics.fmean(ratios) if ratios else None,
            "deferred_histogram": dict(sorted(hist.items(), key=lambda kv: int(kv[0]))),
        }

    return {
        "projects": [{"project": name, **summarise(rs)} for name, rs in sorted(groups.items())],
        "overall": summarise(rows),
    }

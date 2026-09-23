"""The task-events subscriber: gather → enrich → write back → rescore.

Departs from the best-effort DB rule on purpose (spec D7): this handler's
whole job is writing the prioritizer tables, so a DB or Asana failure raises
and Pub/Sub redelivers. A Claude failure never raises — facts and scores
still land, the task is flagged unenriched, and the daily heal retries it.
Design: docs/superpowers/specs/2026-09-23-next-prioritizer-design.md"""

import logging
import time
from datetime import date, datetime, timezone

import clients.asana as asana
import clients.otel as otel
import clients.pubsub as pubsub
from clients.db import get_conn
from models.prioritize import Enrichment, ScoredSet, TaskFacts
from repo import prioritize as repo
from services import custom_fields as cf
from services import enrichment as en
from services import managed_projects, prioritize_config
from services import prioritize as pz
from services.due_digest import today_local

logger = logging.getLogger(__name__)

TOP_N_LOGGED = 10


def handle(message: dict) -> None:
    kind = message.get("kind")
    try:
        if kind == "task_changed":
            handle_task_changed(str(message["gid"]))
        elif kind == "day_changed":
            handle_day_changed()
        else:
            logger.warning("Unknown task-events kind %r — ignoring", kind)
            return
    except Exception:
        otel.prioritize_events.add(1, {"kind": str(kind), "result": "error"})
        raise
    otel.prioritize_events.add(1, {"kind": str(kind), "result": "ok"})


# ---- gather ---------------------------------------------------------------


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def facts_from(
    task: dict, stories: list[dict], *, parent: dict | None = None
) -> tuple[TaskFacts, list[dict]]:
    """A subtask carries no memberships; it inherits its parent's project."""
    ms = (task.get("memberships") or []) or ((parent or {}).get("memberships") or [])
    project = (ms[0].get("project") or {}) if ms else {}
    comments = [
        {
            "text": s.get("text"),
            "created_by": (s.get("created_by") or {}).get("name"),
            "created_at": s.get("created_at"),
        }
        for s in stories
        if s.get("type") == "comment"
    ]
    points, started = cf.read(task)
    name = task.get("name") or ""
    facts = TaskFacts(
        gid=task["gid"],
        project_gid=project.get("gid"),
        project_name=project.get("name"),
        parent_gid=(task.get("parent") or {}).get("gid"),
        name=name,
        permalink_url=task.get("permalink_url"),
        priority=pz.parse_priority(name),
        due_on=_day(task.get("due_on")),
        due_at=_ts(task.get("due_at")),
        start_on=_day(task.get("start_on")),
        started_at=started,
        story_points=points,
        points_estimated=None,  # DB-owned; repo.upsert_facts never overwrites it
        completed=bool(task.get("completed")),
        completed_at=_ts(task.get("completed_at")),
        created_at=_ts(task.get("created_at")) or datetime.now(timezone.utc),
        modified_at=_ts(task.get("modified_at")) or datetime.now(timezone.utc),
        tags=tuple((t.get("name") or "") for t in task.get("tags") or []),
        dependencies=tuple(d["gid"] for d in task.get("dependencies") or [] if d.get("gid")),
        dependents=tuple(d["gid"] for d in task.get("dependents") or [] if d.get("gid")),
        num_open_subtasks=0,  # filled by gather()
        content_hash=en.content_hash(name, task.get("notes") or "", comments),
    )
    return facts, comments


def gather(gid: str) -> list[tuple[TaskFacts, dict, list[dict]]] | None:
    task = asana.get_task_detail(gid, opt_fields=asana.PRIORITIZE_OPT_FIELDS)
    if task is None:
        return None
    stories = asana.get_stories(gid)
    facts, comments = facts_from(task, stories)
    out: list[tuple[TaskFacts, dict, list[dict]]] = []
    open_subs = 0
    if task.get("num_subtasks"):
        for sub in asana.get_subtasks(gid):
            if sub.get("completed"):
                continue
            detail = asana.get_task_detail(sub["gid"], opt_fields=asana.PRIORITIZE_OPT_FIELDS)
            if detail is None:
                continue
            open_subs += 1
            sub_facts, sub_comments = facts_from(detail, asana.get_stories(sub["gid"]), parent=task)
            out.append((sub_facts, detail, sub_comments))
    facts = TaskFacts(**(facts.__dict__ | {"num_open_subtasks": open_subs}))
    return [(facts, task, comments), *out]


# ---- enrich + write back ----------------------------------------------------


def enrich_one(conn, facts: TaskFacts, raw_task: dict, comments: list[dict], today: date) -> str:
    if repo.get_enrichment_hash(conn, facts.gid) == facts.content_hash:
        return "cached"
    try:
        enrichment = en.extract(
            name=facts.name,
            project=facts.project_name,
            html_notes=raw_task.get("html_notes") or "",
            comments=comments,
            due_on=facts.due_on,
            start_on=facts.start_on,
            tags=list(facts.tags),
            today=today,
        )
    except Exception:
        logger.exception("enrichment failed for gid=%s — scoring with defaults", facts.gid)
        otel.errors.add(1, {"handler": "prioritize.enrich"})
        return "failed"
    repo.upsert_enrichment(conn, facts.gid, facts.content_hash, _raw(enrichment), en.MODEL)
    if write_back(conn, facts, enrichment):
        return "written_back"
    return "ok"


def _raw(e: Enrichment) -> dict:
    d = dict(e.__dict__)
    d.pop("unenriched", None)
    if d.get("due_date_inferred"):
        d["due_date_inferred"] = d["due_date_inferred"].isoformat()
    return d


def write_back(conn, facts: TaskFacts, enrichment: Enrichment) -> bool:
    """Spec D6: field empty, never estimated before, conditional claim wins."""
    points = enrichment.story_points_suggested
    if facts.story_points is not None or points is None:
        return False
    if not repo.claim_estimate(conn, facts.gid, points):
        return False
    try:
        cf.set_story_points(facts.gid, points)
        asana.create_story(facts.gid, text=en.estimate_comment(points))
    except Exception:
        logger.exception("story-point write-back failed for gid=%s (claim kept)", facts.gid)
        otel.errors.add(1, {"handler": "prioritize.write_back"})
        return False
    return True


# ---- rescore ----------------------------------------------------------------


def _enrichment_from_raw(raw: dict) -> Enrichment:
    d = dict(Enrichment.DEFAULT.__dict__)
    d.update({k: v for k, v in raw.items() if k in d})
    if isinstance(d.get("due_date_inferred"), str):
        d["due_date_inferred"] = date.fromisoformat(d["due_date_inferred"])
    d["unenriched"] = False
    return Enrichment(**d)


def rescore(conn, *, kind: str, trigger_gid: str | None, today: date) -> ScoredSet:
    t0 = time.monotonic()
    config = prioritize_config.load()
    facts = repo.list_facts(conn)
    enrichments = {
        gid: _enrichment_from_raw(raw) for gid, (hsh, raw) in repo.list_enrichment(conn).items()
    }
    # A stale enrichment (hash moved, model call failed) is still better than
    # defaults; the flag below is what --explain shows.
    scored = pz.score_set(
        facts, enrichments, repo.list_overrides(conn), repo.list_stats(conn), config, today
    )
    current = {f.gid: f.content_hash for f in facts}
    stored = {gid: hsh for gid, (hsh, _) in repo.list_enrichment(conn).items()}
    for t in scored.tasks:
        if stored.get(t.gid) != current.get(t.gid):
            t.components["enrichment_stale"] = True
    repo.replace_scores(conn, scored)
    top = [
        {
            "gid": t.gid,
            "rank": t.rank,
            "score": t.score,
            "components": t.components,
            "started": None,
        }
        for t in scored.next()
        if t.rank is not None
    ]
    if kind != "daily":
        top = top[:TOP_N_LOGGED]
    repo.insert_run(conn, kind=kind, today=today, trigger_gid=trigger_gid, top=top)
    counts: dict[str, int] = {}
    for t in scored.tasks:
        counts[t.bucket] = counts.get(t.bucket, 0) + 1
    for bucket, n in counts.items():
        otel.prioritize_candidates.set(n, {"bucket": bucket})
    otel.prioritize_rescore_duration.record((time.monotonic() - t0) * 1000)
    return scored


# ---- messages ---------------------------------------------------------------


def handle_task_changed(gid: str, *, today: date | None = None) -> None:
    today = today or today_local()
    gathered = gather(gid)
    with get_conn() as conn:
        if gathered is None:
            repo.delete_task(conn, gid)
            otel.prioritize_events.add(1, {"kind": "task_changed", "result": "gone"})
            logger.info("task %s gone from Asana — rows dropped", gid)
        else:
            for facts, raw_task, comments in gathered:
                previous = repo.get_facts(conn, facts.gid)
                repo.upsert_facts(conn, facts)
                if facts.completed:
                    if previous is None or not previous.completed:
                        repo.snapshot_completion(
                            conn,
                            TaskFacts(
                                **(
                                    facts.__dict__
                                    | {
                                        "points_estimated": previous.points_estimated
                                        if previous
                                        else None
                                    }
                                )
                            ),
                        )
                        repo.clear_pin(conn, facts.gid)
                    continue
                merged = TaskFacts(
                    **(
                        facts.__dict__
                        | {"points_estimated": previous.points_estimated if previous else None}
                    )
                )
                result = enrich_one(conn, merged, raw_task, comments, today)
                otel.prioritize_enrich.add(1, {"result": result})
        rescore(conn, kind="event", trigger_gid=gid, today=today)


def settle_deferrals(conn, today: date) -> tuple[int, int]:
    """Spec D8 step 1: yesterday's canonical offers, started or deferred.
    Returns (deferred, started)."""
    run = repo.last_daily_run(conn)
    if not run or not run["top"] or run["today"] >= today:
        return 0, 0
    offered_day: date = run["today"]
    facts = {f.gid: f for f in repo.list_facts(conn)}
    deferred: list[str] = []
    started = 0
    for entry in run["top"]:
        f = facts.get(entry["gid"])
        began = f is not None and (
            (f.started_at is not None and f.started_at >= offered_day)
            or (f.completed_at is not None and f.completed_at.date() >= offered_day)
        )
        entry["started"] = bool(began)
        if began:
            started += 1
        else:
            deferred.append(entry["gid"])
    repo.set_run_top(conn, run["run_id"], run["top"])
    if deferred:
        repo.bump_deferred(conn, deferred, offered_day)
    return len(deferred), started


def heal(conn) -> int:
    """Spec D8 step 2: republish anything Asana knows that we do not."""
    index = repo.list_facts_index(conn)
    enrichment = repo.list_enrichment(conn)

    def needs(gid: str, modified_at: str | None) -> bool:
        if gid not in index:
            return True
        fetched_at, content_hash = index[gid]
        modified = _ts(modified_at)
        if modified and modified > fetched_at:
            return True
        stored = enrichment.get(gid)
        return stored is None or stored[0] != content_hash

    republished = 0
    for project_gid in sorted(managed_projects.gids()):
        for task in asana.list_project_tasks(
            project_gid, only_open=True, opt_fields=asana.HEAL_OPT_FIELDS
        ):
            candidates = [task]
            if task.get("num_subtasks"):
                candidates += [s for s in asana.get_subtasks(task["gid"]) if not s.get("completed")]
            for t in candidates:
                if needs(t["gid"], t.get("modified_at")):
                    pubsub.publish_task_changed(t["gid"], "heal")
                    republished += 1
    return republished


def handle_day_changed(*, today: date | None = None) -> dict:
    today = today or today_local()
    with get_conn() as conn:
        deferred, started = settle_deferrals(conn, today)
        healed = heal(conn)
        rescore(conn, kind="daily", trigger_gid=None, today=today)
    logger.info(
        "day_changed %s: %d deferred, %d started, %d republished", today, deferred, started, healed
    )
    return {"deferred": deferred, "started": started, "healed": healed}

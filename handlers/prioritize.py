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
# Levels of subtask nesting walked: project resolution hops up at most this
# many parents; gather and heal descend at most this many levels below a task.
MAX_SUBTASK_DEPTH = 3


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


def _project_name(task: dict, project_gid: str) -> str | None:
    for m in task.get("memberships") or []:
        project = m.get("project") or {}
        if project.get("gid") == project_gid:
            return project.get("name")
    return None


def _project(task: dict, parent: dict | None) -> tuple[str | None, str | None]:
    """(gid, name): the first of task, parent whose project_of() is managed;
    failing that, the first that has any project at all."""
    sources = [t for t in (task, parent) if t]
    managed = managed_projects.gids()
    resolved = [(t, managed_projects.project_of(t)) for t in sources]
    for t, gid in resolved:
        if gid and gid in managed:
            return gid, _project_name(t, gid)
    for t, gid in resolved:
        if gid:
            return gid, _project_name(t, gid)
    return None, None


def _resolve_project(task: dict) -> tuple[str | None, str | None, int]:
    """(gid, name, hops): the task's project, walking up the parent chain — a
    subtask has no memberships, and neither does a level-1 subtask's child's
    parent, so a single parent hop leaves a grandchild with no project. Stops
    at the first managed project, at MAX_SUBTASK_DEPTH hops, or at a parent
    Asana 404s; falls back to the nearest unmanaged project any hop had.

    `hops` is how far up the walk went (0 for a top-level task): the task's
    own level in its tree, so gather can measure its descent from the tree
    top exactly as heal does. Unresolved, it is the hops actually walked."""
    first_any: tuple[str | None, str | None] = (None, None)
    current = task
    depth = 0
    while True:
        gid, name = _project(current, None)
        if gid and gid in managed_projects.gids():
            return gid, name, depth
        if gid and first_any[0] is None:
            first_any = (gid, name)
        parent_ref = current.get("parent") or {}
        if not parent_ref.get("gid") or depth >= MAX_SUBTASK_DEPTH:
            return first_any[0], first_any[1], depth
        parent = asana.get_task_detail(parent_ref["gid"], opt_fields=asana.PRIORITIZE_OPT_FIELDS)
        if parent is None:
            return first_any[0], first_any[1], depth
        current = parent
        depth += 1


def facts_from(
    task: dict,
    stories: list[dict],
    *,
    parent: dict | None = None,
    inherited: tuple[str | None, str | None] | None = None,
) -> tuple[TaskFacts, list[dict]]:
    """The task's project, resolved as managed_projects.project_of does; a
    subtask with no managed membership of its own takes its parent's, and
    failing that the `inherited` (gid, name) its gathering root resolved."""
    project_gid, project_name = _project(task, parent)
    if inherited is not None and (
        project_gid is None or project_gid not in managed_projects.gids()
    ):
        if inherited[0] is not None:
            project_gid, project_name = inherited
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
        project_gid=project_gid,
        project_name=project_name,
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


def _gather_subtasks(
    task: dict,
    project: tuple[str | None, str | None],
    level: int,
    out: list[tuple[TaskFacts, dict, list[dict]]],
) -> int:
    """Append the open subtasks of `task` (and theirs, while level stays
    within MAX_SUBTASK_DEPTH) to `out`, each carrying the root's project.
    Returns the number of open direct subtasks."""
    if not task.get("num_subtasks") or level > MAX_SUBTASK_DEPTH:
        return 0
    open_subs = 0
    for sub in asana.get_subtasks(task["gid"]):
        if sub.get("completed"):
            continue
        detail = asana.get_task_detail(sub["gid"], opt_fields=asana.PRIORITIZE_OPT_FIELDS)
        if detail is None:
            continue
        open_subs += 1
        sub_facts, sub_comments = facts_from(
            detail, asana.get_stories(sub["gid"]), inherited=project
        )
        idx = len(out)
        out.append((sub_facts, detail, sub_comments))
        n = _gather_subtasks(detail, project, level + 1, out)
        out[idx] = (
            TaskFacts(**(sub_facts.__dict__ | {"num_open_subtasks": n})),
            detail,
            sub_comments,
        )
    return open_subs


def gather(gid: str) -> list[tuple[TaskFacts, dict, list[dict]]] | None:
    task = asana.get_task_detail(gid, opt_fields=asana.PRIORITIZE_OPT_FIELDS)
    if task is None:
        return None
    stories = asana.get_stories(gid)
    # gid may itself be a subtask at any level (e.g. republished directly by
    # heal); it carries no memberships, so its project comes up the chain.
    project_gid, project_name, level = _resolve_project(task)
    facts, comments = facts_from(task, stories, inherited=(project_gid, project_name))
    out: list[tuple[TaskFacts, dict, list[dict]]] = []
    # Depth counts from the tree top, as heal's walk does: gathering a level-1
    # subtask descends to level MAX_SUBTASK_DEPTH, never beyond, so it cannot
    # store rows heal never lists (which heal would then republish, fail to
    # resolve, and delete — an oscillation).
    open_subs = _gather_subtasks(task, (facts.project_gid, facts.project_name), level + 1, out)
    facts = TaskFacts(**(facts.__dict__ | {"num_open_subtasks": open_subs}))
    return [(facts, task, comments), *out]


# ---- enrich + write back ----------------------------------------------------


def enrich_one(
    conn, facts: TaskFacts, raw_task: dict, comments: list[dict], today: date
) -> tuple[Enrichment | None, str]:
    """(enrichment or None, result label). A cache hit returns the stored
    enrichment — so a write-back that could not claim last time (e.g. the
    field was missing) is retried — and None only when the model failed."""
    stored = repo.get_enrichment(conn, facts.gid)
    if stored is not None and stored[0] == facts.content_hash:
        return _enrichment_from_raw(stored[1]), "cached"
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
        return None, "failed"
    repo.upsert_enrichment(conn, facts.gid, facts.content_hash, _raw(enrichment), en.MODEL)
    return enrichment, "ok"


def _raw(e: Enrichment) -> dict:
    d = dict(e.__dict__)
    d.pop("unenriched", None)
    if d.get("due_date_inferred"):
        d["due_date_inferred"] = d["due_date_inferred"].isoformat()
    return d


def claim_write_back(conn, facts: TaskFacts, enrichment: Enrichment) -> int | None:
    """Spec D6, inside the transaction: field empty, a suggestion, the field
    resolvable, and the conditional claim wins → the points to write back
    once the transaction has committed. A missing field claims nothing, so
    the next event retries."""
    points = enrichment.story_points_suggested
    if facts.story_points is not None or facts.points_estimated is not None or points is None:
        return None
    try:
        cf.field_gid(cf.STORY_POINTS)
    except RuntimeError as exc:
        logger.warning(
            "not claiming an estimate for gid=%s: %s (run scripts/setup_custom_fields.py)",
            facts.gid,
            exc,
        )
        return None
    if not repo.claim_estimate(conn, facts.gid, points):
        return None
    return points


def write_back(gid: str, points: int) -> bool:
    """After commit: field first, then the comment. A failure logs and keeps
    the claim — the estimate is never written twice (D6)."""
    try:
        cf.set_story_points(gid, points)
        asana.create_story(gid, text=en.estimate_comment(points))
    except Exception:
        logger.exception("story-point write-back failed for gid=%s (claim kept)", gid)
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
    repo.lock_rescore(conn)  # held to commit: concurrent rescores serialise
    config = prioritize_config.load()
    facts = repo.list_facts(conn)
    enrichments = {
        gid: _enrichment_from_raw(raw) for gid, (hsh, raw) in repo.list_enrichment(conn).items()
    }
    # A stale enrichment (hash moved, model call failed) is still better than
    # defaults; the flag below is what --explain shows.
    scored = pz.score_set(
        facts,
        enrichments,
        repo.list_overrides(conn),
        repo.list_stats(conn),
        config,
        today,
        project_last_offered=repo.project_last_offered(conn, today=today),
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
    """Transaction A (facts, enrichment, claim) commits before the Asana
    write-back, so a failed rescore can never roll back a claim whose
    estimate is already in Asana; transaction B rescores."""
    today = today or today_local()
    gathered = gather(gid)
    # An excluded project (D15) is gathered and stored but never ranked, so
    # the model call and the points write-back would be spend for nothing. A
    # task moved out of it enriches on that move's own event.
    excluded = (
        gathered is not None
        and gathered[0][0].project_name in prioritize_config.load().excluded_projects
    )
    if gathered is not None and gathered[0][0].project_gid not in managed_projects.gids():
        with get_conn() as conn:
            repo.delete_task(conn, gid)
        otel.prioritize_events.add(1, {"kind": "task_changed", "result": "unmanaged"})
        logger.info("task %s is in no managed project — rows dropped", gid)
        return
    to_write: list[tuple[str, int]] = []
    results: list[tuple[str, str]] = []
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
                if excluded:
                    results.append((facts.gid, "skipped"))
                    continue
                merged = TaskFacts(
                    **(
                        facts.__dict__
                        | {"points_estimated": previous.points_estimated if previous else None}
                    )
                )
                enrichment, result = enrich_one(conn, merged, raw_task, comments, today)
                points = claim_write_back(conn, merged, enrichment) if enrichment else None
                if points is not None:
                    to_write.append((facts.gid, points))
                    result = "claimed"
                results.append((facts.gid, result))
    # Transaction A has committed: the claim stands whatever Asana does now.
    written = {g for g, points in to_write if write_back(g, points)}
    if written:
        # Persist the points now: a subtask never gets the echo event that
        # would otherwise carry them back, and the rescore below should see them.
        with get_conn() as conn:
            for g, points in to_write:
                if g in written:
                    repo.set_story_points(conn, g, points)
    for g, result in results:
        if result == "claimed":
            result = "written_back" if g in written else "ok"
        otel.prioritize_enrich.add(1, {"result": result})
    with get_conn() as conn:
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
        # Started at any date and not completed = in progress, not deferred.
        began = f is not None and (
            f.started_at is not None
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


def _open_descendants(task: dict, level: int) -> list[dict]:
    """Open subtasks below `task`, down to MAX_SUBTASK_DEPTH levels — one
    compact get_subtasks listing per parent per level, no detail fetches."""
    if not task.get("num_subtasks") or level > MAX_SUBTASK_DEPTH:
        return []
    found: list[dict] = []
    for s in asana.get_subtasks(task["gid"], opt_fields=asana.HEAL_OPT_FIELDS):
        if s.get("completed"):
            continue
        found.append(s)
        found += _open_descendants(s, level + 1)
    return found


def heal() -> int:
    """Spec D8 step 2: republish anything Asana knows that we do not, and
    anything we still hold open that Asana's open-task listing no longer
    mentions — a completion or deletion whose event was lost. Reads in its
    own short transaction, then lists and publishes outside any."""
    with get_conn() as conn:
        index = repo.list_facts_index(conn)
        enrichment = repo.list_enrichment(conn)
        open_gids = repo.list_open_gids(conn)

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
    seen: set[str] = set()
    for project_gid in sorted(managed_projects.gids()):
        for task in asana.list_project_tasks(
            project_gid, only_open=True, opt_fields=asana.HEAL_OPT_FIELDS
        ):
            candidates = [task, *_open_descendants(task, 1)]
            for t in candidates:
                seen.add(t["gid"])
                if needs(t["gid"], t.get("modified_at")):
                    pubsub.publish_task_changed(t["gid"], "heal")
                    republished += 1
    for gid in open_gids - seen:
        pubsub.publish_task_changed(gid, "heal")
        republished += 1
    return republished


def handle_day_changed(*, today: date | None = None) -> dict:
    today = today or today_local()
    with get_conn() as conn:
        deferred, started = settle_deferrals(conn, today)
    healed = heal()
    with get_conn() as conn:
        rescore(conn, kind="daily", trigger_gid=None, today=today)
    logger.info(
        "day_changed %s: %d deferred, %d started, %d republished", today, deferred, started, healed
    )
    return {"deferred": deferred, "started": started, "healed": healed}

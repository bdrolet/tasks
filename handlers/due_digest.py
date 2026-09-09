"""Due-day digest rebuild: POST /digest on the webhook CF, ticked by Cloud
Scheduler every 10 minutes. Decides whether a rebuild is due, lists Asana,
routes and condenses each task, diffs against due_day_events, and applies
the diff through schedule-api. Called only from main.py.
Design: docs/superpowers/specs/2026-09-03-due-day-digest-design.md."""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import httpx

import clients.asana as asana
import clients.otel as otel
import clients.schedule_api as sapi
from clients.db import get_conn
from models.digest import DigestEvent, DigestTask
from repo import due_digest as repo
from services import due_digest as dd
from services import task_bullets as tb

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=60)
PRUNE_AFTER = timedelta(days=90)
_DIGEST_TITLE_RE = re.compile(r"^\d+ tasks? due$")
_LIST_WORKERS = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def should_rebuild(state: dict, now: datetime, force: bool) -> bool:
    if force:
        return True
    last = state.get("last_rebuilt_at")
    if last is None or now - last > STALE_AFTER:
        return True
    dirty = state.get("dirty_at")
    return dirty is not None and dirty > last


class _RepoCache:
    """tb.BulletCache backed by task_bullets; one connection per rebuild."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def get(self, gid: str, content_hash: str) -> list[str] | None:
        row = repo.get_bullets(self._conn, gid)
        return row["bullets"] if row and row["content_hash"] == content_hash else None

    def put(self, gid: str, content_hash: str, points: list[str]) -> None:
        repo.put_bullets(self._conn, gid, content_hash, points)


def _list_candidates() -> list[dict]:
    projects = asana.list_projects()
    with ThreadPoolExecutor(max_workers=_LIST_WORKERS) as pool:
        per_project = list(
            pool.map(
                lambda p: asana.list_project_tasks(
                    p["gid"], only_open=True, opt_fields=asana.DIGEST_OPT_FIELDS
                ),
                projects,
            )
        )
    raw = [t for batch in per_project for t in batch] + asana.list_my_tasks(
        only_open=True, opt_fields=asana.DIGEST_OPT_FIELDS
    )
    seen: set[str] = set()
    out: list[dict] = []
    for t in raw:
        if t["gid"] in seen:
            continue
        seen.add(t["gid"])
        out.append(t)
    return out


_ROUTING_ENV = {
    "family_project_gid": "ASANA_PROJECT_FAMILY_GID",
    "family_calendar_id": "CALENDAR_FAMILY_ID",
    "shared_calendar_id": "CALENDAR_SHARED_ID",
}


def _routing() -> dict:
    cfg = {key: os.environ.get(env, "") for key, env in _ROUTING_ENV.items()}
    for key, value in cfg.items():
        if not value:
            logger.warning("Digest routing: %s unset — that rule is skipped", _ROUTING_ENV[key])
    return {
        "project_calendars": [(cfg["family_project_gid"], cfg["family_calendar_id"])],
        "shared_calendar_id": cfg["shared_calendar_id"],
    }


def _digest_tasks(candidates: list[dict], today: date, conn, counts: dict) -> list[DigestTask]:
    routing = _routing()
    cache = _RepoCache(conn)
    budget = tb.Budget()
    tasks: list[DigestTask] = []
    for t in candidates:
        if not dd.in_window(t, today):
            continue
        notes = t.get("html_notes") or ""
        points, result = tb.points_for(t["gid"], t["name"], notes, cache=cache, budget=budget)
        otel.digest_bullet_calls.add(1, {"result": result})
        if result in ("ok", "fallback"):
            counts["bullet_calls"] += 1
        tasks.append(
            DigestTask(
                gid=t["gid"],
                name=t["name"],
                permalink_url=t.get("permalink_url") or "",
                due_on=t["due_on"],
                calendar_id=dd.route(t, **routing),
                points=points,
                links=tb.parse_links(notes),
            )
        )
    return tasks


def _adopt(event: DigestEvent) -> str | None:
    """An existing digest event on that day/calendar (lost row) — reuse it."""
    for hit in sapi.search_digest_events(calendar=event.calendar_id, day=event.day):
        if _DIGEST_TITLE_RE.match(hit.get("title") or ""):
            return hit["event_id"]
    return None


def _create(event: DigestEvent, conn, counts: dict) -> None:
    event_id = _adopt(event)
    if event_id:
        sapi.patch_event(
            event_id, calendar=event.calendar_id, title=event.title, sections=event.sections
        )
        counts["adopted"] += 1
        otel.digest_events.add(1, {"op": "adopt"})
    else:
        created = sapi.create_event(
            calendar=event.calendar_id, day=event.day, title=event.title, sections=event.sections
        )
        event_id = created["event_id"]
        counts["created"] += 1
        otel.digest_events.add(1, {"op": "create"})
    repo.upsert_event(
        conn,
        day=event.day,
        calendar_id=event.calendar_id,
        event_id=event_id,
        content_hash=event.content_hash(),
        task_gids=event.task_gids,
    )


def _update(event: DigestEvent, row: dict, conn, counts: dict) -> None:
    try:
        sapi.patch_event(
            row["event_id"], calendar=event.calendar_id, title=event.title, sections=event.sections
        )
    except sapi.NotFound:
        logger.info("Digest event %s gone from calendar — recreating", row["event_id"])
        repo.delete_event(conn, day=event.day, calendar_id=event.calendar_id)
        _create(event, conn, counts)
        return
    counts["updated"] += 1
    otel.digest_events.add(1, {"op": "update"})
    repo.upsert_event(
        conn,
        day=event.day,
        calendar_id=event.calendar_id,
        event_id=row["event_id"],
        content_hash=event.content_hash(),
        task_gids=event.task_gids,
    )


def _delete(row: dict, conn, counts: dict) -> None:
    sapi.delete_event(row["event_id"], calendar=row["calendar_id"])
    repo.delete_event(conn, day=row["day"], calendar_id=row["calendar_id"])
    counts["deleted"] += 1
    otel.digest_events.add(1, {"op": "delete"})


def _apply(plan, conn, counts: dict) -> None:
    """Only calendar (HTTP) failures are per-pair survivable. A DB error aborts
    the whole rebuild: in one pg8000 transaction every later statement fails
    too, so continuing would issue calendar writes whose rows all roll back."""
    for event in plan.creates:
        try:
            _create(event, conn, counts)
        except httpx.HTTPError:
            counts["errors"] += 1
            otel.digest_errors.add(1, {"stage": "calendar"})
            logger.exception("Digest create failed for %s/%s", event.day, event.calendar_id)
    for event, row in plan.updates:
        try:
            _update(event, row, conn, counts)
        except httpx.HTTPError:
            counts["errors"] += 1
            otel.digest_errors.add(1, {"stage": "calendar"})
            logger.exception("Digest update failed for %s/%s", event.day, event.calendar_id)
    for row in plan.deletes:
        try:
            _delete(row, conn, counts)
        except httpx.HTTPError:
            counts["errors"] += 1
            otel.digest_errors.add(1, {"stage": "calendar"})
            logger.exception("Digest delete failed for %s/%s", row["day"], row["calendar_id"])


def run(*, force: bool = False) -> dict:
    now = _now()
    started = now  # stamped as last_rebuilt_at below — see mark_rebuilt call
    try:
        with get_conn() as conn:
            state = repo.get_state(conn)
    except Exception:
        logger.exception("Digest: DB unavailable — skipping rebuild")
        otel.digest_rebuilds.add(1, {"outcome": "db_unavailable"})
        return {"outcome": "db_unavailable"}
    if not should_rebuild(state, now, force):
        otel.digest_rebuilds.add(1, {"outcome": "skipped"})
        return {"outcome": "skipped"}
    if not (os.environ.get("SCHEDULE_API_URL") and os.environ.get("SCHEDULE_API_TOKEN")):
        logger.error("SCHEDULE_API_URL / SCHEDULE_API_TOKEN unset — digest cannot run")
        otel.digest_rebuilds.add(1, {"outcome": "error"})
        return {"outcome": "error"}

    today = dd.today_local(now)
    counts = {
        "tasks": 0,
        "created": 0,
        "updated": 0,
        "deleted": 0,
        "adopted": 0,
        "bullet_calls": 0,
        "errors": 0,
    }
    with otel.get_tracer().start_as_current_span("digest.rebuild") as span:
        try:
            candidates = _list_candidates()
        except Exception:
            logger.exception("Digest: Asana listing failed — nothing changed")
            otel.digest_errors.add(1, {"stage": "list"})
            otel.digest_rebuilds.add(1, {"outcome": "error"})
            return {"outcome": "error", **counts}

        try:
            with get_conn() as conn:
                tasks = _digest_tasks(candidates, today, conn, counts)
                counts["tasks"] = len(tasks)
                desired = dd.build_events(tasks)
                stored = repo.list_events(conn, since=today)
                plan = dd.plan(desired, stored, today)
                _apply(plan, conn, counts)
                repo.prune_events(conn, before=today - PRUNE_AFTER)
                # Stamped with the rebuild's START: a webhook that fires while we
                # were listing Asana leaves dirty_at > last_rebuilt_at, so the next
                # tick picks it up (spec D8).
                repo.mark_rebuilt(conn, at=started)
        except Exception:
            logger.exception("Digest: rebuild failed")
            otel.digest_errors.add(1, {"stage": "rebuild"})
            otel.digest_rebuilds.add(1, {"outcome": "error"})
            return {"outcome": "error", **counts}

        outcome = "partial" if counts["errors"] else "ok"
        span.set_attribute("digest.today", today.isoformat())
        for key, value in counts.items():
            span.set_attribute(f"digest.{key}", value)
    otel.digest_rebuilds.add(1, {"outcome": outcome})
    logger.info("Digest rebuild %s: %s", outcome, counts)
    return {"outcome": outcome, **counts}

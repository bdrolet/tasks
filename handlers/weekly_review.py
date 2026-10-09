"""Monday's review (spec D11): build GET /review's body from the stored
rows, render it, and post it as a comment on one standing task so the
cadence lives in the list. Best-effort on the DB like the digest: without
rows there is nothing to say, so a DB failure reports and does not raise."""

import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import review, sections
from services.due_digest import today_local

logger = logging.getLogger(__name__)

REVIEW_EXTERNAL = "review:weekly"
REVIEW_TASK_NAME = "[P2] Weekly strategy review"


def _standing_task() -> str:
    gid = asana.find_task_by_external(REVIEW_EXTERNAL)
    if gid:
        return gid
    created = asana.create_task_from_fields(
        {
            "name": REVIEW_TASK_NAME,
            "html_notes": "<body>The weekly strategy review lands here as a comment every Monday. "
            "Never complete this task; it is the review's home.</body>",
            "projects": [asana.ASANA_PROJECT_ID],
            "external": {"gid": REVIEW_EXTERNAL, "data": "tasks"},
        }
    )
    section = sections.for_category("review")
    if section:
        asana.add_task_to_section(created.gid, section)
    return created.gid


def run() -> dict:
    today = today_local()
    try:
        with get_conn() as conn:
            strategy = repo_goals.load_snapshot(conn)
            states = repo_goals.get_states(conn, today)
            if not states:
                states = repo_goals.get_states(conn, today.fromordinal(today.toordinal() - 1))
            scores = repo.list_scores(conn)
            suppressed = repo_sup.list_necessity(conn)
    except Exception:
        logger.exception("weekly review: DB unavailable — skipping")
        otel.errors.add(1, {"handler": "weekly_review"})
        return {"outcome": "db_unavailable"}
    if not strategy.goals:
        return {"outcome": "no_strategy"}
    body = review.build(strategy, states, scores, suppressed, today)
    gid = _standing_task()
    asana.create_story(gid, text=review.render(body))
    logger.info(
        "weekly review posted on %s (%d goals, %d grooming, %d stop-doing)",
        gid,
        len(body["goals"]),
        len(body["grooming"]),
        len(body["stop_doing"]["tasks"]),
    )
    return {"outcome": "posted", "task_gid": gid}

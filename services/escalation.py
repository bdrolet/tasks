"""Overdue-task escalation: incomplete tasks past their due date move to the
Overdue section (and optionally get a tag). Triggered by Cloud Scheduler via
POST /escalate on the tasks-webhook CF.

Escalation is one-time per task: once `escalated_at` is set, a task never
re-escalates even if it later re-enters Overdue-eligible state (e.g. after a
label move takes it out of Overdue and it becomes overdue again)."""

import hmac
import logging
import os

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import tasks as repo_tasks
from services import sections

logger = logging.getLogger(__name__)


def _was_escalated(task_gid: str) -> bool:
    try:
        with get_conn() as conn:
            return repo_tasks.was_escalated(conn, task_gid)
    except Exception:
        logger.exception("escalated_at lookup failed for gid=%s", task_gid)
        return False  # degrade to section-only dedupe


def _mark_escalated(task_gid: str) -> None:
    try:
        with get_conn() as conn:
            repo_tasks.mark_escalated(conn, task_gid)
    except Exception:
        logger.exception("escalated_at update failed for gid=%s", task_gid)


def is_authorized(authorization_header: str | None) -> bool:
    """Bearer-token check for the Cloud Scheduler-triggered /escalate route.

    The webhook CF must stay publicly invokable for Asana's unauthenticated
    webhook posts, so this route can't rely on IAM invoker restrictions —
    it needs its own app-level check, same pattern as the Asana signature
    validation in handlers/asana_webhook.py."""
    token = os.environ.get("ASANA_ESCALATE_TOKEN", "")
    if not token:
        logger.warning("ASANA_ESCALATE_TOKEN not set — rejecting /escalate request")
        return False
    expected = f"Bearer {token}"
    return hmac.compare_digest(expected, authorization_header or "")


def run() -> dict[str, int]:
    overdue_gid = sections.overdue()
    tag_gid = os.environ.get("ASANA_OVERDUE_TAG_GID", "")
    if not overdue_gid and not tag_gid:
        logger.warning(
            "Neither ASANA_SECTION_OVERDUE_GID nor ASANA_OVERDUE_TAG_GID set — nothing to do"
        )
        return {"scanned": 0, "escalated": 0}

    tasks = asana.get_incomplete_tasks_past_due()
    escalated = 0
    for task in tasks:
        current = asana.current_section(task)
        if overdue_gid and current and current["gid"] == overdue_gid:
            continue  # already in Overdue
        if _was_escalated(task["gid"]):
            continue  # already escalated on a previous run
        if overdue_gid:
            asana.add_task_to_section(task["gid"], overdue_gid)
            otel.tasks_moved.add(
                1,
                {
                    "from_section": current["name"] if current else "unknown",
                    "to_section": "Overdue",
                },
            )
        if tag_gid:
            asana.add_tag(task["gid"], tag_gid)
        _mark_escalated(task["gid"])
        otel.escalations.add(1)
        escalated += 1

    logger.info("Escalation scan: %d overdue, %d escalated", len(tasks), escalated)
    return {"scanned": len(tasks), "escalated": escalated}

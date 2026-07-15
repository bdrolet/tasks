import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from models.events import LabelAppliedEvent
from repo import tasks as repo_tasks
from services import sections

logger = logging.getLogger(__name__)


def _resolve_task_gid(event: LabelAppliedEvent) -> str | None:
    """Event task_gid → tasks DB row → Asana external-ID addressing."""
    if event.get("task_gid"):
        return event["task_gid"]
    try:
        with get_conn() as conn:
            gid = repo_tasks.get_gid_by_message(conn, event["message_id"])
        if gid:
            return gid
    except Exception:
        logger.exception("DB lookup failed for message_id=%s", event["message_id"])
    return asana.find_task_by_external(event["message_id"])


def handle(event: LabelAppliedEvent) -> None:
    section_gid = sections.for_category(event["label"])
    if not section_gid:
        logger.info("Label %r has no section mapping — nothing to do", event["label"])
        return

    task_gid = _resolve_task_gid(event)
    if not task_gid:
        logger.warning("No Asana task found for message_id=%s — skipping", event["message_id"])
        return

    task = asana.get_task(task_gid)
    current = asana.current_section(task)
    if current and current["gid"] == section_gid:
        logger.info("Task %s already in %s — no move needed", task_gid, current["name"])
        return

    asana.add_task_to_section(task_gid, section_gid)
    otel.tasks_moved.add(
        1,
        {
            "from_section": current["name"] if current else "unknown",
            "to_section": event["label"],
        },
    )
    logger.info("Task %s moved to %s section (label from %s)", task_gid, event["label"], event["source"])

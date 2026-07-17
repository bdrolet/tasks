import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import tasks as repo_tasks
from services import sections

logger = logging.getLogger(__name__)


def handle(task_gid: str) -> None:
    # Asana fires "changed/completed" on both complete AND un-complete — verify.
    task = asana.get_task(task_gid)
    if not task.get("completed"):
        logger.info("Task %s not completed (uncomplete event?) — ignoring", task_gid)
        return

    otel.tasks_completed.add(1)

    try:
        with get_conn() as conn:
            repo_tasks.mark_completed(conn, task_gid)
    except Exception:
        logger.exception("completed_at update failed for gid=%s", task_gid)

    done_gid = sections.done()
    if not done_gid:
        logger.warning("ASANA_SECTION_DONE_GID not set — task %s left in place", task_gid)
        return

    current = asana.current_section(task)
    if current and current["gid"] == done_gid:
        logger.info("Task %s already in Done — no move needed", task_gid)
        return

    asana.add_task_to_section(task_gid, done_gid)
    otel.tasks_moved.add(
        1,
        {"from_section": current["name"] if current else "unknown", "to_section": "Done"},
    )
    logger.info("Task %s completed — moved to Done", task_gid)

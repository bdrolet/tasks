import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import task_index as repo_index
from repo import tasks as repo_tasks
from services import managed_projects, recurrence, sections

logger = logging.getLogger(__name__)


def handle(task_gid: str) -> None:
    # Asana fires "changed/completed" on both complete AND un-complete — verify.
    task = asana.get_task(task_gid)
    if not task.get("completed"):
        try:
            with get_conn() as conn:
                repo_index.set_completed(conn, task_gid, False)
        except Exception:
            logger.exception("task_index uncomplete update failed for gid=%s", task_gid)
        logger.info("Task %s not completed (uncomplete event) — index flag cleared", task_gid)
        return

    project_gid = managed_projects.project_of(task)

    otel.tasks_completed.add(1)

    # Recurrence runs before the Done move: current_section is how the
    # successor learns where to live, and the move overwrites it. Guarded —
    # a recurrence failure must never cost us the completion itself. find_rule
    # is inside the guard too: it indexes into Asana-supplied tag dicts and
    # parse() ultimately calls int() on attacker-controlled text, so it can
    # raise just like the rest of the recurrence step.
    try:
        rule = recurrence.find_rule(task.get("tags") or [])
        if rule:
            detail = asana.get_task_detail(task_gid) or {}
            recurrence.spawn_next(task, detail, asana.current_section(task), rule)
    except Exception:
        logger.exception("Recurrence failed for gid=%s — completion continues", task_gid)

    try:
        with get_conn() as conn:
            repo_tasks.mark_completed(conn, task_gid)
            repo_index.set_completed(conn, task_gid, True)
    except Exception:
        logger.exception("completed_at update failed for gid=%s", task_gid)

    # A subtask has no project membership, so there is no Done section it
    # could belong to — moving it would add it to a project it is not in.
    if task.get("parent"):
        logger.info("Task %s is a subtask — completed, no Done move", task_gid)
        return

    done_gid = sections.done(project_gid)
    if not done_gid:
        logger.warning("No Done section for project %s — task %s left in place", project_gid, task_gid)
        return

    current = asana.current_section(task, project_gid)
    if current and current["gid"] == done_gid:
        logger.info("Task %s already in Done — no move needed", task_gid)
        return

    asana.add_task_to_section(task_gid, done_gid)
    otel.tasks_moved.add(
        1,
        {"from_section": current["name"] if current else "unknown", "to_section": "Done"},
    )
    logger.info("Task %s completed — moved to Done", task_gid)

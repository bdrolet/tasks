import logging
import re

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from models.events import EmailClassifiedEvent
from repo import tasks as repo_tasks
from services import deadline, email_summary, policy, sections, tags, task_content

logger = logging.getLogger(__name__)


def handle(event: EmailClassifiedEvent) -> None:
    if not policy.warrants_task(event):
        logger.info(
            "No task for category=%r — message_id=%s", event["category"], event["message_id"]
        )
        return

    # Enrichment: generated summary first, invite seeds from inbox appended.
    summary = email_summary.generate(event)
    key_points = summary.key_points + (event.get("seed_key_points") or [])
    relevant_links = summary.relevant_links + (event.get("seed_links") or [])

    due_date = None
    if event["importance"] in ("P0", "P1"):
        try:
            due_date = deadline.extract_deadline(event)
        except Exception:
            logger.exception("Deadline extraction failed for message_id=%s", event["message_id"])

    tag_gids = tags.resolve_gids(event.get("tags") or [])
    html_notes = task_content.render_html_notes(
        task_content.for_email(event, key_points, relevant_links)
    )
    # Prepend the [PX] prefix per the "Title" section of docs/task-content-standard.md
    # (authoritative — doc wins over code). The model should return only {verb} {object};
    # strip a leading [Pn] tag defensively so the authoritative prefix is applied once.
    # Falls back to [PX] {subject} in create_task when there is no enriched title.
    if summary.title:
        body = re.sub(r"^\[P\d\]\s*", "", summary.title).strip()
        title = f"[{event['importance']}] {body}"
    else:
        title = None
    task = asana.create_task(
        event,
        tag_gids=tag_gids,
        due_date=due_date,
        html_notes=html_notes,
        title=title,
    )
    if task is None:
        logger.info(
            "Task not created (unconfigured or duplicate) — message_id=%s", event["message_id"]
        )
        return

    otel.tasks_created.add(1, {"category": event["category"], "importance": event["importance"]})

    try:
        with get_conn() as conn:
            repo_tasks.insert(
                conn,
                task_gid=task.gid,
                message_id=event["message_id"],
                category=event["category"],
                importance=event["importance"],
            )
    except Exception:
        # The Asana task already exists — a DB hiccup must not crash the event
        # (a Pub/Sub retry would duplicate-skip in Asana and still miss the row;
        # label_applied's external-GID fallback covers the gap).
        logger.exception("tasks row insert failed for gid=%s", task.gid)

    section_gid = sections.for_category(event["category"])
    if section_gid:
        asana.add_task_to_section(task.gid, section_gid)

    logger.info(
        "Task created gid=%s category=%s section=%s message_id=%s",
        task.gid,
        event["category"],
        section_gid,
        event["message_id"],
    )

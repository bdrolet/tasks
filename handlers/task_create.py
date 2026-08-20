import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from models.events import Decision, EmailClassifiedEvent
from repo import suppressions as repo_suppressions
from repo import tasks as repo_tasks
from services import (
    deadline,
    email_summary,
    policy,
    sections,
    tags,
    task_content,
    task_index,
    triage,
)

logger = logging.getLogger(__name__)


def _suppress(
    event: EmailClassifiedEvent,
    *,
    reason: str,
    source: str,
    related_task_gid: str | None,
    evidence: list,
    resolves: bool = False,
) -> None:
    """Gate-2 outcome: no task. Optionally attach the email to a related task
    as a comment, then record. Every step is best-effort — the decision was
    made on evidence and a recording failure never reverses it.

    The gate never closes a task. When the email settles the related task's
    matter (resolves=True) the comment says so and asks Ben to close it — the
    judgement is the model's, the decision stays his, and an open task with a
    "close me" comment is obvious on review in a way a silent close is not."""
    if related_task_gid:
        lead = "Looks resolved — close this task if you agree." if resolves else "Related email:"
        try:
            asana.create_story(
                related_task_gid,
                text=f"{lead} {event['subject']} — {reason} — {event.get('web_link') or ''}".rstrip(
                    " —"
                ),
            )
        except Exception:
            logger.exception(
                "related-task comment failed gid=%s message_id=%s",
                related_task_gid,
                event["message_id"],
            )
    try:
        with get_conn() as conn:
            repo_suppressions.insert(
                conn,
                message_id=event["message_id"],
                category=event["category"],
                importance=event["importance"],
                subject=event.get("subject"),
                sender=event.get("sender"),
                reason=reason,
                source=source,
                related_task_gid=related_task_gid,
                evidence=evidence,
            )
    except Exception:
        logger.exception("suppressed_emails insert failed message_id=%s", event["message_id"])
    otel.tasks_suppressed.add(
        1,
        {
            "category": event["category"],
            "importance": event["importance"],
            "source": source,
            "attached": "true" if related_task_gid else "false",
            "resolves": "true" if resolves else "false",
        },
    )
    logger.info(
        "Task suppressed source=%s related=%s resolves=%s message_id=%s reason=%s",
        source,
        related_task_gid,
        resolves,
        event["message_id"],
        reason,
    )


def handle(event: EmailClassifiedEvent) -> None:
    if not policy.warrants_task(event):
        logger.info(
            "No task for category=%r — message_id=%s", event["category"], event["message_id"]
        )
        return

    decision: Decision = triage.decide(event)
    if not decision.actionable or decision.related_task_gid:
        _suppress(
            event,
            reason=decision.reason,
            source="agent",
            related_task_gid=decision.related_task_gid,
            evidence=decision.evidence,
            resolves=decision.resolves,
        )
        return

    # Enrichment: generated summary first, invite seeds from inbox appended.
    summary = email_summary.generate(event)
    phrase = policy.no_action_phrase(summary.key_points)
    if phrase:
        _suppress(event, reason=phrase, source="phrase", related_task_gid=None, evidence=[])
        return
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
    # Prepend the authoritative [PX] prefix per the "Title" section of
    # docs/task-content-standard.md (doc wins over code). email_summary already
    # produced a clean {verb} {object} (no priority tag); create_task falls back
    # to [PX] {subject} when there is no enriched title.
    title = f"[{event['importance']}] {summary.title}" if summary.title else None
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

    # Index for semantic search — best-effort by construction (refresh
    # swallows all failures).
    task_index.refresh(task.gid)

    logger.info(
        "Task created gid=%s category=%s section=%s message_id=%s",
        task.gid,
        event["category"],
        section_gid,
        event["message_id"],
    )

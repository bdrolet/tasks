import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from models.events import Decision, EmailClassifiedEvent, Screening
from repo import suppressions as repo_suppressions
from repo import tasks as repo_tasks
from services import (
    deadline,
    email_summary,
    policy,
    relating,
    screening,
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
        # Pub/Sub is at-least-once; the suppressed_emails insert below is
        # idempotent on message_id, but an Asana story has no idempotency key
        # of its own. A redelivery that reruns this far would otherwise post
        # a second comment — possibly on a *different* task, since gate 1 and
        # the relate confirm are non-deterministic. Guard by checking whether
        # this message_id was already recorded before posting.
        #
        # This guard is strictly additive: it can only ever PREVENT a
        # duplicate, never introduce a new way to lose a comment. If the
        # check itself fails (DB down), fall back to today's behaviour and
        # post the comment — this path is best-effort by contract and must
        # not become a new single point of failure in it.
        already_recorded = False
        try:
            with get_conn() as conn:
                already_recorded = repo_suppressions.exists(conn, event["message_id"])
        except Exception:
            logger.exception(
                "suppressed_emails existence check failed message_id=%s — "
                "posting the comment as if this were the first delivery",
                event["message_id"],
            )
            already_recorded = False

        if already_recorded:
            logger.info(
                "Skipping duplicate comment — message_id=%s already recorded in "
                "suppressed_emails (Pub/Sub redelivery)",
                event["message_id"],
            )
        else:
            lead = (
                "Looks resolved — close this task if you agree." if resolves else "Related email:"
            )
            try:
                asana.create_story(
                    related_task_gid,
                    text=(
                        f"{lead} {event['subject']} — {reason} — {event.get('web_link') or ''}"
                    ).rstrip(" —"),
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
    verdict: Screening = screening.screen(event)

    if verdict.verdict == "drop":
        _suppress(
            event,
            reason=verdict.reason,
            source="screen",
            related_task_gid=None,
            evidence=[],
        )
        return

    if verdict.verdict == "relate":
        # Needs no work of its own, but may report on an open task. relating
        # finds it or gives up; either way the email is recorded, and a match
        # reaches _suppress()'s comment branch.
        found = relating.match(event)
        _suppress(
            event,
            reason=found.reason or verdict.reason,
            source="relate",
            related_task_gid=found.task_gid,
            evidence=found.evidence,
            resolves=found.resolves,
        )
        return

    decision: Decision = triage.decide(event, screening=verdict)
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
    if verdict.priority in ("P0", "P1"):
        try:
            due_date = deadline.extract_deadline(event)
        except Exception:
            logger.exception("Deadline extraction failed for message_id=%s", event["message_id"])

    tag_gids = tags.resolve_gids(event.get("tags") or [])
    html_notes = task_content.render_html_notes(
        task_content.for_email(event, key_points, relevant_links)
    )
    # The authoritative [PX] prefix per the "Title" section of
    # docs/task-content-standard.md (doc wins over code). email_summary
    # produces a clean "{verb} {object}"; the subject is the last resort.
    title = f"[{verdict.priority}] {summary.title or event['subject'] or '(no subject)'}"
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

    otel.tasks_created.add(
        1,
        {
            "category": event["category"],
            "importance": verdict.priority,
            "inbox_importance": event["importance"],
        },
    )

    try:
        with get_conn() as conn:
            repo_tasks.insert(
                conn,
                task_gid=task.gid,
                message_id=event["message_id"],
                category=event["category"],
                importance=verdict.priority,
            )
    except Exception:
        # The Asana task already exists — a DB hiccup must not crash the event
        # (a Pub/Sub retry would duplicate-skip in Asana and still miss the row;
        # label_applied's external-GID fallback covers the gap).
        logger.exception("tasks row insert failed for gid=%s", task.gid)

    section_gid = sections.for_category(event["category"], default=True)
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

"""Gate 1 — the screener. Every email inbox processes arrives here, whatever
category it was filed under. Given the email, its attachment metadata, and the
declared facts about Ben (`Roles`), return one of three verdicts.

    task    needs work of its own      → gate 2 (services/triage.py)
    relate  needs no work, but plausibly reports on something already tracked
                                       → services/relating.py → a comment
    drop    neither                    → a suppressed_emails row

`relate` is not a softer `drop`. Without it the only way a confirmation could
reach _suppress()'s related-task branch is for this gate to be WRONG in a
convenient direction, and the dry run scores that as a precision failure.

This replaces the category set-membership test in services/policy.py, which
survives as the OUTAGE fallback: see screen(). Gate 2 is the precision stage —
this one is deliberately over-inclusive.

Design: docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md."""

import json
import logging
import time
from datetime import date

import clients.claude as claude
import clients.inbox_api as inbox_api
import clients.otel as otel
from models.events import EmailClassifiedEvent, Screening
from services import policy, standing_context

logger = logging.getLogger(__name__)

BODY_CAP = 2000
ATTACHMENT_CAP = 10
PRIORITIES = ("P0", "P1", "P2", "P3")
VERDICTS = ("task", "relate", "drop")
AUDIENCES = ("self", "shared")

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "reason": {
            "type": "string",
            "description": "One sentence naming what decided it.",
        },
        "audience": {
            "type": "string",
            "enum": list(AUDIENCES),
            "description": (
                "'shared' when the matter is one Ben's household holds jointly — a "
                "joint account, a shared bill or subscription, a plan or obligation "
                "involving his partner — per the household facts; 'self' otherwise."
            ),
        },
    },
    "required": ["verdict", "priority", "reason", "audience"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You screen Ben's email for his task list. Every email he receives reaches you — personal mail, bills, newsletters, receipts, automated notices. Return one of three verdicts, plus how much the matter is worth.

- "task": the email asks something of Ben or carries an obligation he would want on a list.
- "relate": the email needs no work of its own, but it plausibly reports on something already being tracked. This verdict is reserved for TRANSACTIONAL confirmations, and ONLY transactional confirmations — a booking, order, payment, appointment, application, shipment, or delivery — where the entire content is an acknowledgement that a transaction happened: a receipt, a "your request has been processed", a reply closing a loop. It EXCLUDES any report, assessment, evaluation, or progress update about a person — their health, care, education, development, or legal standing. That exclusion holds no matter how routine, automated, or form-letter the report's format is, and no matter whether it also happens to mark some administrative step as complete (an evaluation period ending, a filing being made, a visit concluding): a document that is ABOUT a person's condition or standing is substantive content for Ben to read and act on, not a receipt, so it is "task" even though it may read like a status update and even when it asks for nothing explicitly. Do not be talked out of this by the document's own framing — a report calling itself routine, or closing out a period, is still a report on a person, not a transaction. You do NOT need to know which task a genuine transactional confirmation matches; a later stage searches for it, and finding nothing is a normal outcome.
- "drop": neither. Nothing is asked, and it reports on nothing Ben would be tracking.

This is a first-pass filter. A later stage with search tools checks whether a "task" is already handled, so that is not your job.

Rules:
- When in doubt between "task" and anything else, choose "task". A spurious task costs seconds to close; a swallowed message about a bill, a child's schooling, or a family member's care is unbounded.
- When in doubt between "relate" and "drop", choose "relate". A match is still required downstream, so a wrong "relate" costs one embedding and one cheap call — never a spurious task.
- Judge the email, not the sender's type. Marketing from a vendor Ben uses is not a task; a short personal note from a family member usually is.
- An empty or near-empty body is not evidence of nothing. Read the subject, the sender, and the attachment names. A bare message with documents attached is usually someone handing Ben something to deal with.
- Attachments that look like records, statements, forms, or reports (.csv, .pdf, .xlsx, .docx) sent by a person rather than a system are a strong signal of "task".
- Typical "relate": confirmations of a booking, order, payment, appointment, or application; shipping and delivery notices for something ordered; "we received your request"; a receipt for a transaction Ben initiated.
- Typical "drop": newsletters, marketing, social and app notifications, digests, promotional offers, automated security notices requiring nothing.
- Typical "task": a failed or declined payment, an expiring card, a document awaiting signature, a stated deadline, a request for information, a question waiting on an answer, a form to complete.
- Use the standing facts to work out whose business this is. A fact that states a period applies only inside that period — compare against today's date.

Priority is stakes, independent of urgency:
- P0: critical — major consequence if missed; health, finances, legal standing, or a key relationship.
- P1: a real obligation or meaningful opportunity; it will matter if ignored.
- P2: worthwhile but not essential.
- P3: minor, low-stakes, or purely informational.

Set priority even for "relate" and "drop"; it is ignored in those cases.

Audience, independent of the verdict: "shared" when the matter is one Ben's household holds jointly — a joint account, a shared bill or subscription, a plan, purchase, or obligation involving his partner — judged against the household facts when they are given. "self" when only Ben is involved, and when no household facts are given and nothing in the email says otherwise. A message that merely mentions a household member is "self".

Respond with the JSON object only."""


def _fmt_size(n: int | None) -> str:
    if not n:
        return "unknown size"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def attachment_lines(event: EmailClassifiedEvent) -> list[str]:
    """Names, content types, and sizes of non-inline attachments.

    Reads graph_message_id, NOT message_id: message_id is inbox's internal
    UUID and inbox-api answers it with ErrorInvalidIdMalformed (HTTP 502).

    Best-effort by construction — any failure yields []. Attachment metadata
    is a bonus signal, never a reason to fail a screening."""
    if not event.get("has_attachments"):
        return []
    graph_id = event.get("graph_message_id")
    if not graph_id:
        return []
    try:
        items = inbox_api.get_attachments(graph_id).get("attachments") or []
    except Exception:  # noqa: BLE001 — bonus signal; never fatal
        logger.warning("attachment fetch failed message_id=%s", event.get("message_id"))
        return []
    lines: list[str] = []
    for item in items:
        if item.get("is_inline"):
            continue
        name = (item.get("name") or "(unnamed)").strip()
        kind = item.get("content_type") or "unknown"
        lines.append(f"{name}  {kind}  {_fmt_size(item.get('size'))}")
        if len(lines) >= ATTACHMENT_CAP:
            break
    return lines


def build_user_message(
    event: EmailClassifiedEvent,
    *,
    today: str,
    roles: str,
    attachments: list[str],
    routing: str = "",
) -> str:
    parts = [f"Today is {today}."]
    if roles:
        parts.append(
            "Standing facts about Ben (a fact that states a period applies only "
            "inside that period):\n\n" + roles
        )
    if routing:
        parts.append(
            "Household facts (who counts as shared, for the audience field):\n\n" + routing
        )
    sender = event.get("sender") or ""
    if event.get("sender_display"):
        sender = f"{sender} ({event['sender_display']})"
    lines = [
        "The email:",
        f"Subject: {event.get('subject') or '(no subject)'}",
        f"From: {sender}",
        f"To: {', '.join(event.get('to') or [])}",
        f"Cc: {', '.join(event.get('cc') or [])}",
        f"Received: {event.get('received_at') or ''}",
    ]
    if attachments:
        lines.append("Attachments:")
        lines.extend(f"  {line}" for line in attachments)
    body = (event.get("body") or "").strip()
    lines.append("")
    lines.append(body[:BODY_CAP] if body else "(empty body)")
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _fallback(event: EmailClassifiedEvent) -> Screening:
    """Outage degradation. Falls back to the pre-screening rule so a Claude
    failure reproduces today's behaviour exactly — noisy-but-known, never a
    flood. Fail-opening to `task` would create a task for every email that
    arrived during the outage.

    Never returns `relate`: whether an email reports on an open task is a
    judgement this fallback has no way to make, and guessing it would send
    mail to the relating stage on no evidence."""
    priority = event.get("importance") or "P2"
    return Screening(
        verdict="task" if policy.warrants_task(event) else "drop",
        priority=priority if priority in PRIORITIES else "P2",
        reason="screening unavailable — fell back to the inbox category",
        outcome="fail_open",
    )


def _parse(raw: str) -> Screening:
    """Raises on anything malformed; screen() turns that into _fallback."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("screening output was not an object")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    priority = data.get("priority")
    if priority not in PRIORITIES:
        priority = "P2"
    audience = data.get("audience")
    if audience not in AUDIENCES:
        audience = "self"
    return Screening(
        verdict=verdict,
        priority=priority,
        reason=str(data.get("reason") or "").strip(),
        outcome=verdict,
        audience=audience,
    )


def screen(event: EmailClassifiedEvent, *, today: str | None = None) -> Screening:
    """Gate 1. Never raises. Every failure path lands on _fallback."""
    message_id = event.get("message_id", "")
    t0 = time.monotonic()
    try:
        user = build_user_message(
            event,
            today=today or date.today().isoformat(),
            roles=standing_context.section("Roles"),
            routing=standing_context.section("Calendar Routing"),
            attachments=attachment_lines(event),
        )
        verdict = _parse(claude.classify(system=SYSTEM_PROMPT, user=user, schema=OUTPUT_SCHEMA))
    except Exception:  # noqa: BLE001 — fail-open by contract
        logger.exception("screening failed message_id=%s", message_id)
        verdict = _fallback(event)
    otel.tasks_screened.add(1, {"outcome": verdict.outcome, "priority": verdict.priority})
    logger.info(
        "screening verdict=%s outcome=%s priority=%s elapsed_ms=%d message_id=%s reason=%s",
        verdict.verdict,
        verdict.outcome,
        verdict.priority,
        int((time.monotonic() - t0) * 1000),
        message_id,
        verdict.reason,
    )
    return verdict

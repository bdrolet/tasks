"""The `relate` outcome — find the open task an email reports on.

Gate 1 says a task MAY exist; this stage finds it or gives up. Cost discipline
is the constraint: `relate` lands on the receipt/confirmation population, which
is large, so a Sonnet tool-runner per email is not affordable. Instead:
nearest-neighbour over the embedding corpus that already exists
(services/task_index.py keeps it fresh), a similarity floor, one Haiku call to
confirm, and an Asana fetch to verify whatever it names.

No match is a NORMAL outcome, not a failure. Match() falls through to a plain
suppressed_emails row, which is still strictly better than the single log line
these emails leave today.

This stage never closes a task. It supplies the gid; handlers/task_create.py
::_suppress renders the comment, and its wording asks Ben to close.

Design: docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md."""

import json
import logging

import clients.asana as asana
import clients.claude as claude
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from models.events import EmailClassifiedEvent, Match
from repo import task_index as repo_index

logger = logging.getLogger(__name__)

BODY_CAP = 2000
NOTES_CAP = 300
CANDIDATES = 3

# Cosine score below which the best neighbour is not worth a confirm call.
# This is the precision knob. A comment on the WRONG task is read as fact
# about that task and has no cheap recovery, so it starts conservative.
# Tuned from a measured run, not guessed: scripts/backtest_screening.py replayed
# 1,266 historical emails and logged every relate row's best score. No relate
# row scored below 0.60, so 0.55 never fired; the 0.60-0.65 band held 46 rows
# and every one was a no-match. Raising the floor to 0.65 skips those 46
# confirm calls and loses zero matches against that run.
SIMILARITY_FLOOR = 0.65

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "task_gid": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "One of the candidate gids, verbatim, or null.",
        },
        # Positioned before `resolves` in property order. Empirically, moving
        # the verb-check reasoning here (out of `reason`, which used to carry
        # it) and generating it ahead of the boolean recovered resolves
        # accuracy that was lost when `reason` was shortened (see the prompt
        # bullet below) — measured 9/19 false-resolves without this field,
        # back to ~4-6/19 with it, reproduced across probe runs. That is an
        # observed effect, NOT a documented API guarantee: Anthropic's
        # structured-output docs do not promise fields are generated in
        # declaration order, and no probe isolated field-order from
        # field-existence (no run tested `verb_check` positioned after
        # `resolves`). Treat the ordering as a working hypothesis that
        # happens to test well today, not a contract to lean on further.
        # Internal only: never read into Match, never logged anywhere a
        # person would see it as a comment — see the prompt bullet on reason
        # for why that separation exists.
        "verb_check": {"type": "string"},
        "resolves": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["task_gid", "verb_check", "resolves", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are matching an email to an open Asana task. The email has already been judged to need no work of its own; the only question is whether it reports on something Ben is already tracking.

You are shown up to three candidate tasks, nearest first by meaning. They are candidates, not answers — they are the closest things in the corpus, which is not the same as being about the same matter.

Rules:
- Return a task_gid ONLY when the email and the task are the same matter: same vendor, same reservation or order number, same amount, same instrument, same thread, same saga. "Both are about travel" is not the same matter.
- null is the expected answer. Most emails of this kind match nothing. Returning null costs nothing; naming the wrong task puts a false statement on a real task, which Ben reads as fact and which has no cheap undo. When the candidates are merely adjacent, return null.
- task_gid must be one of the candidate gids shown, copied verbatim. Never invent one.
- Before setting resolves, find the verb the task's own title uses for what it is waiting on — book, pay, ship, refund, verify, check, confirm, review, decide, or similar. That verb is the ONLY thing resolves answers. It is not asking "did something related happen" or "is this good news about the same matter" — a task can accumulate many true, relevant, on-topic emails without any of them being its resolution. Work this out in verb_check, one or two short sentences — it is internal scratch space, never shown to Ben, but it is not unlimited either: name the verb and state plainly whether this email is that verb's outcome or merely activity nearby, then stop. Do the reasoning there, not in reason.
- If the task's verb is verify, check, confirm, review, decide, or another word for a person examining something, resolves is false, full stop, no matter how final the email sounds. This holds even when the email reports the exact event the task is skeptical of — e.g. a task that says "confirm the payment actually posted, don't just trust the app's pending status" or "verify the package was actually delivered, not just marked shipped" exists BECAUSE a status notice, on its own, was already judged insufficient; an email carrying that same category of notice is not new evidence, it is the very thing the task doubts. A receipt, shipment notice, cancellation, or renewal shows that a transaction occurred, never that Ben looked at it and signed off.
- If the task's verb is itself a transactional outcome — book it, pay it, ship it, get the refund — resolves is true only when this exact email is the record of THAT specific transaction (the same order, amount, instrument, or reservation the task names) completing. A different or merely similar-sounding transaction — another item shipping, a different order canceling, an unrelated subscription renewing — does not resolve it.
- Default to false. Ben reads resolves as "close this task", so do not claim one you have not seen exact evidence of.
- reason is published verbatim as a comment on the Asana task — Ben will read it there as a note, not as your reasoning. Write one short sentence, well under 140 characters: name what makes this email and the task the same matter (or, when task_gid is null, why nothing matched). Do NOT use reason to explain or justify the resolves decision above — that judgment is made by the rules, not narrated in the output.

Respond with the JSON object only."""


def _candidates(event: EmailClassifiedEvent) -> list[dict]:
    """Top open-task neighbours by cosine similarity, best first."""
    text = f"{event.get('subject') or ''}\n\n{(event.get('body') or '')[:BODY_CAP]}"
    embedding = vertex.embed(text, task_type="RETRIEVAL_QUERY")
    with get_conn() as conn:
        hits = repo_index.semantic_candidates(
            conn,
            query_embedding=embedding,
            completed=False,  # a completed task needs no "looks resolved" comment
            due_before=None,
            due_after=None,
            project=None,
            limit=CANDIDATES,
        )
        scores = {h["task_gid"]: h["score"] for h in hits}
        rows = repo_index.get_rows(conn, list(scores))
    for row in rows:
        row["score"] = scores.get(row["task_gid"]) or 0.0
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def build_user_message(event: EmailClassifiedEvent, rows: list[dict]) -> str:
    body = (event.get("body") or "").strip()
    parts = [
        "The email:",
        f"Subject: {event.get('subject') or '(no subject)'}",
        f"From: {event.get('sender') or ''}",
        f"Received: {event.get('received_at') or ''}",
        "",
        body[:BODY_CAP] if body else "(empty body)",
        "",
        "Open tasks that may be the same matter:",
    ]
    for row in rows:
        parts.append(
            f"- task_gid: {row['task_gid']}  (similarity {row['score']:.2f})\n"
            f"  title: {row.get('title') or ''}\n"
            f"  notes: {(row.get('notes') or '')[:NOTES_CAP]}\n"
            f"  due_on: {row.get('due_on') or 'none'}"
        )
    return "\n".join(parts)


def _confirm(event: EmailClassifiedEvent, rows: list[dict]) -> Match:
    raw = claude.classify(
        system=SYSTEM_PROMPT, user=build_user_message(event, rows), schema=OUTPUT_SCHEMA
    )
    data = json.loads(raw)
    reason = str(data.get("reason") or "").strip()
    gid = data.get("task_gid") or None

    # The model may only choose from what it was shown. A gid outside the
    # candidate set is a hallucination, and acting on it would comment on a
    # task nobody compared against this email.
    if gid is not None and str(gid) not in {r["task_gid"] for r in rows}:
        logger.warning("relate confirm named a gid outside the candidates: %s", gid)
        gid = None
    if gid is not None and not asana.task_exists(str(gid)):
        gid = None
    if gid is None:
        return Match(reason=reason or "no candidate matched")
    return Match(
        task_gid=str(gid),
        resolves=bool(data.get("resolves")),
        reason=reason,
        evidence=[{"kind": "task", "ref": str(gid), "note": reason}],
    )


def match(event: EmailClassifiedEvent, *, rows: list[dict] | None = None) -> Match:
    """Never raises. Any failure — Vertex, Postgres, Haiku, Asana — returns
    Match(), so the email is recorded as a plain suppression and no comment is
    posted. Worst-case outage behaviour is today's behaviour.

    `rows`, when given, is used in place of an internal `_candidates(event)`
    lookup — pass `None` (the default) to fetch candidates here exactly as
    before. This exists so the offline dry run
    (scripts/backtest_screening.py) can fetch candidates once and hand them
    in, letting it exercise this exact function end to end instead of a
    hand-copy of its control flow. Production's only call site
    (handlers/task_create.py) always calls `match(event)` with no `rows`, so
    that path is unaffected."""
    message_id = event.get("message_id", "")
    if rows is None:
        try:
            rows = _candidates(event)
        except Exception:  # noqa: BLE001 — no-match is the degradation
            logger.exception("relate candidate lookup failed message_id=%s", message_id)
            rows = []

    if not rows or rows[0]["score"] < SIMILARITY_FLOOR:
        best = rows[0]["score"] if rows else None
        logger.info("relate below floor best=%s message_id=%s", best, message_id)
        otel.tasks_related.add(1, {"matched": "false"})
        return Match(reason="no open task above the similarity floor")

    try:
        result = _confirm(event, rows)
    except Exception:  # noqa: BLE001
        logger.exception("relate confirm failed message_id=%s", message_id)
        otel.tasks_related.add(1, {"matched": "false"})
        return Match(reason="relate confirm unavailable")

    otel.tasks_related.add(1, {"matched": "true" if result.task_gid else "false"})
    logger.info(
        "relate matched=%s resolves=%s message_id=%s reason=%s",
        result.task_gid,
        result.resolves,
        message_id,
        result.reason,
    )
    return result

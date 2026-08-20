"""Gate 2 — the triage agent. Given a classified email, declared facts about
Ben (context/standing-context.md, `Roles`), and read-only search/fetch tools
over mail and tasks, decide whether the email still requires anything of him.
Mirrors what /searching-inbox-emails and /searching-tasks do by hand.

Design: docs/superpowers/specs/2026-08-18-standing-context-gate-design.md.
Every tool is a thin delegation to an existing client and returns a JSON
string; backend failures come back as {"error": ...} so one outage degrades
the agent's evidence instead of aborting the run. decide() is fail-open."""

import contextvars
import json
import logging
import time
from datetime import date
from html.parser import HTMLParser

from anthropic import beta_tool

import clients.asana as asana
import clients.claude as claude
import clients.inbox_api as inbox_api
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from models.events import Decision, EmailClassifiedEvent
from repo import task_index as repo_index
from repo import tasks as repo_tasks
from services import standing_context

logger = logging.getLogger(__name__)

CURRENT_MESSAGE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "triage_message_id", default=None
)

EMAIL_BODY_CAP = 2000
TASK_NOTES_CAP = 600
MAX_RESULTS = 25


def _err(exc: BaseException) -> str:
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _count(tool: str) -> None:
    otel.triage_tool_calls.add(1, {"tool": tool})


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    p = _TextOnly()
    try:
        p.feed(html)
    except Exception:
        return html
    return " ".join("".join(p.parts).split())


@beta_tool
def search_emails(query: str, mode: str = "graph", limit: int = 10) -> str:
    """Search Ben's mailboxes. Call this when the email describes a recurring
    obligation (a bill, renewal, subscription, payment) to see whether prior
    mail from the same sender shows it is already automated or already paid
    ("automatic payment", "thanks for your payment"), or when you need the
    rest of a thread. Returns a JSON list of {message_id, subject, sender,
    received_at, preview, category, importance}; pass a message_id to
    get_email for the full body.

    Args:
        query: In graph mode, Outlook KQL — `from:billing@xfinity.com`,
            `subject:refund`, or plain keywords. In db mode, plain keywords.
        mode: "graph" (live Outlook, all mailboxes — default) or "db"
            (already-processed mail, includes category/importance).
        limit: Max results, 1-25.
    """
    _count("search_emails")
    try:
        rows = inbox_api.search(query, mode=mode, limit=max(1, min(limit, MAX_RESULTS)))
        keep = ("message_id", "subject", "sender", "received_at", "preview", "category", "importance")
        return json.dumps([{k: r.get(k) for k in keep} for r in rows], default=str)
    except Exception as exc:  # noqa: BLE001 — tool contract: never raise
        return _err(exc)


@beta_tool
def get_email(message_id: str) -> str:
    """Fetch one email's full content by message_id (from search_emails or
    from the triage request). Use it to read a prior message's body when the
    preview is not enough. Returns JSON {subject, sender, to, cc, received_at,
    body} with the body capped at 2000 characters.

    Args:
        message_id: Graph message id.
    """
    _count("get_email")
    try:
        e = inbox_api.get_email(message_id)
        body = e.get("body") or ""
        if (e.get("body_type") or "").lower() == "html":
            body = _strip_html(body)
        return json.dumps(
            {
                "subject": e.get("subject"),
                "sender": e.get("from_email"),
                "to": [r.get("address") for r in e.get("to") or []],
                "cc": [r.get("address") for r in e.get("cc") or []],
                "received_at": e.get("received_at"),
                "body": body[:EMAIL_BODY_CAP],
            },
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@beta_tool
def search_tasks(
    query: str, semantic: bool = False, completed: bool | None = None, limit: int = 10
) -> str:
    """Search Ben's Asana tasks — open AND completed by default. Call this
    when the email could be a follow-up on something already tracked (a
    refund, a dispute, a thread that already produced a task) or when the
    email is a reply on a thread. Any task created from THIS email's thread is
    listed first with same_email=true. Returns a JSON list of {task_gid,
    title, notes, project, completed, due_on, permalink_url[, score]}; pass a
    task_gid to get_task for the description and comments.

    Args:
        query: Keywords (vendor, amount, subject words). With semantic=true,
            a natural-language description of the matter.
        semantic: true for nearest-neighbour ranking by meaning; false for
            substring match on title/notes.
        completed: null = both (default), false = open only, true = done only.
        limit: Max results, 1-25.
    """
    _count("search_tasks")
    limit = max(1, min(limit, MAX_RESULTS))
    try:
        with get_conn() as conn:
            if semantic:
                emb = vertex.embed(query, task_type="RETRIEVAL_QUERY")
                hits = repo_index.semantic_candidates(
                    conn,
                    query_embedding=emb,
                    completed=completed,
                    due_before=None,
                    due_after=None,
                    project=None,
                    limit=limit,
                )
                scores = {h["task_gid"]: h["score"] for h in hits}
                rows = repo_index.get_rows(conn, list(scores))
                for r in rows:
                    r["score"] = scores.get(r["task_gid"])
                rows.sort(key=lambda r: r["score"] or 0, reverse=True)
            else:
                rows = repo_index.substring_candidates(
                    conn, query=query, completed=completed, limit=limit
                )
            mid = CURRENT_MESSAGE_ID.get()
            own_gid = repo_tasks.get_gid_by_message(conn, mid) if mid else None
            if own_gid:
                own = [r for r in rows if r["task_gid"] == own_gid]
                rows = [r for r in rows if r["task_gid"] != own_gid]
                own_rows = own or repo_index.get_rows(conn, [own_gid])
                for r in own_rows:
                    r["same_email"] = True
                rows = own_rows + rows
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    return json.dumps([dict(r) for r in rows], default=str)


@beta_tool
def get_task(task_gid: str) -> str:
    """Fetch one Asana task's description, state, and recent comments by
    task_gid (from search_tasks). Call it before setting related_task_gid so
    you are sure it is the same matter. Returns JSON {name, notes, completed,
    due_on, permalink_url, comments:[{text, created_at, by}]}.

    Args:
        task_gid: Asana task GID.
    """
    _count("get_task")
    try:
        t = asana.get_task_detail(task_gid)
        if t is None:
            return json.dumps({"error": "not found"})
        stories = asana.get_stories(task_gid)
        comments = [
            {
                "text": s.get("text"),
                "created_at": s.get("created_at"),
                "by": (s.get("created_by") or {}).get("name"),
            }
            for s in stories
            if s.get("type") == "comment"
        ][-5:]
        return json.dumps(
            {
                "name": t.get("name"),
                "notes": (t.get("notes") or "")[:TASK_NOTES_CAP],
                "completed": bool(t.get("completed")),
                "due_on": t.get("due_on"),
                "permalink_url": t.get("permalink_url"),
                "comments": comments,
            },
            default=str,
        )
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


TOOLS = [search_emails, get_email, search_tasks, get_task]


MAX_ITERATIONS = 6
DEADLINE_S = 60.0
BODY_CAP = 3000

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean"},
        "reason": {
            "type": "string",
            "description": "One sentence naming the fact, email, or task that decided it.",
        },
        "related_task_gid": {
            "type": ["string", "null"],
            "description": "GID of an existing task covering the same matter, else null.",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["email", "task", "fact", "thread"]},
                    "ref": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["kind", "ref", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["actionable", "reason", "related_task_gid", "evidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You triage Ben's email for his task list. An email has already been classified as task-worthy on its own terms. Your job is to decide whether, given everything you can find out, it still requires anything of Ben. You have read-only tools to search and read his mail and his Asana tasks; use them when the email's shape calls for it (recurring bills and renewals → prior mail from the sender; refunds, disputes, follow-ups, replies → existing tasks), and skip them when the email is plainly a new request.

Rules:
- Set actionable to false ONLY when evidence you actually have — a standing fact that currently applies, prior mail, an existing task, or the thread itself — clearly shows the email requires nothing of Ben. If unsure, actionable is true.
- A standing fact that states a period applies only inside that period; compare against today's date.
- A reply to a thread Ben started, or a message where Ben is only on cc and the content is acknowledgment ("thanks for letting us know", "sounds good", "best wishes"), is not a task.
- "no action required", "no action is needed", "for your records", "automatic payment", "autopay", "will be charged automatically" — in this email or in prior mail from the same sender — disqualifies a payment or review task.
- The sender's schedule (a statement posted, a payment series ending, a feature auto-enabling on a date) is not Ben's deadline. Broadcast vendor announcements default to not actionable. Never treat "Action required" as present unless the source says it.
- If an existing task covers the same matter — same vendor/amount/instrument within small variance, same thread, same saga — set related_task_gid to it, open or completed, but only after you have fetched it (get_task) or seen enough of it to be sure. If the email shows a closed matter has regressed, leave related_task_gid null and set actionable true.
- Name every lookup or fact you relied on in evidence (kind: email|task|fact|thread; ref: message_id, task gid, fact heading, or "thread"; note: what it showed).
- Do not reason beyond the facts given and the evidence you retrieved.

Respond with the JSON object only."""


def build_user_message(event: EmailClassifiedEvent, *, today: str, roles: str) -> str:
    parts = [f"Today is {today}."]
    if roles:
        parts.append(
            "Standing facts about Ben (some state the period they apply to; a fact "
            "whose period has passed does not apply):\n\n" + roles
        )
    parts.append(
        "The email:\n"
        f"Subject: {event.get('subject') or ''}\n"
        f"From: {event.get('sender') or ''}"
        + (f" ({event['sender_display']})" if event.get("sender_display") else "")
        + "\n"
        f"To: {', '.join(event.get('to') or [])}\n"
        f"Cc: {', '.join(event.get('cc') or [])}\n"
        f"Received: {event.get('received_at') or ''}\n"
        f"Classified: {event.get('category')} / {event.get('importance')}\n"
        f"Message id: {event.get('message_id')}\n\n"
        f"{(event.get('body') or '')[:BODY_CAP]}"
    )
    return "\n\n".join(parts)


def _fail_open(reason: str, message_id: str) -> Decision:
    logger.warning("triage fail-open (%s) message_id=%s", reason, message_id)
    return Decision(outcome="fail_open")


def _gid_exists(task_gid: str) -> bool:
    """The spec's fail-open case: a related_task_gid we cannot fetch is
    treated as no match. Any Asana failure counts as 'cannot fetch'."""
    try:
        return asana.get_task_detail(task_gid) is not None
    except Exception:  # noqa: BLE001 — unfetchable is unfetchable
        logger.warning("related_task_gid %s not fetchable — ignoring", task_gid)
        return False


def _parse(text: str | None, stop: str, message_id: str, *, gid_exists=_gid_exists) -> Decision:
    if stop != "end_turn" or not text:
        return _fail_open(stop, message_id)
    try:
        data = json.loads(text)
    except ValueError:
        return _fail_open("unparseable", message_id)
    if not isinstance(data, dict) or not isinstance(data.get("actionable"), bool):
        return _fail_open("schema", message_id)
    reason = str(data.get("reason") or "").strip()
    gid = data.get("related_task_gid") or None
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    actionable = data["actionable"]
    if gid is not None and not gid_exists(str(gid)):
        gid = None
    if gid is not None:
        if not reason:
            return _fail_open("no_reason", message_id)
        return Decision(actionable=False, reason=reason, related_task_gid=str(gid), evidence=evidence, outcome="attached")
    if actionable:
        return Decision(actionable=True, reason=reason, evidence=evidence, outcome="actionable")
    if not reason:
        return _fail_open("no_reason", message_id)
    return Decision(actionable=False, reason=reason, evidence=evidence, outcome="suppressed")


def decide(event: EmailClassifiedEvent, *, today: str | None = None) -> Decision:
    """Gate 2. Never raises; every failure returns the actionable default."""
    if event.get("category") == "urgent":
        return Decision()
    message_id = event["message_id"]
    today = today or date.today().isoformat()
    roles = standing_context.section("Roles")
    user = build_user_message(event, today=today, roles=roles)
    token = CURRENT_MESSAGE_ID.set(message_id)
    t0 = time.monotonic()
    try:
        text, stop = claude.run_agent(
            system=SYSTEM_PROMPT,
            user=user,
            tools=TOOLS,
            output_schema=OUTPUT_SCHEMA,
            max_iterations=MAX_ITERATIONS,
            deadline_s=DEADLINE_S,
        )
        decision = _parse(text, stop, message_id)
    except Exception:  # noqa: BLE001 — fail-open by contract
        logger.exception("triage agent failed message_id=%s", message_id)
        decision = Decision(outcome="fail_open")
    finally:
        CURRENT_MESSAGE_ID.reset(token)
    otel.triage_duration.record((time.monotonic() - t0) * 1000, {"outcome": decision.outcome})
    logger.info(
        "triage outcome=%s actionable=%s related=%s message_id=%s reason=%s",
        decision.outcome, decision.actionable, decision.related_task_gid, message_id, decision.reason,
    )
    return decision

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
from html.parser import HTMLParser

from anthropic import beta_tool

import clients.asana as asana
import clients.inbox_api as inbox_api
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from repo import task_index as repo_index
from repo import tasks as repo_tasks

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
    except Exception as exc:  # noqa: BLE001 — tool contract: never raise
        return _err(exc)
    keep = ("message_id", "subject", "sender", "received_at", "preview", "category", "importance")
    return json.dumps([{k: r.get(k) for k in keep} for r in rows], default=str)


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
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
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
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
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


TOOLS = [search_emails, get_email, search_tasks, get_task]

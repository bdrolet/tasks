"""Semantic-index maintenance — one concern: keep task_index in sync with
Asana task content. `refresh` is the single best-effort write path used by
the pipeline handler, the API routers, and the webhook; `index_task_dict`
is the bulk path for the backfill script (task dicts already in hand)."""

import hashlib
import logging

import clients.asana as asana
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from repo import task_index as repo_index

logger = logging.getLogger(__name__)


def content_hash(title: str, notes: str) -> str:
    return hashlib.sha256(f"{title}\n{notes}".encode()).hexdigest()


def _project_name(task: dict) -> str | None:
    ms = task.get("memberships") or []
    if not ms:
        return None
    return (ms[0].get("project") or {}).get("name")


def index_task_dict(conn, task: dict, *, embed_fn=None) -> bool:
    """Upsert one Asana task dict (listing or detail shape). Embeds only when
    content changed or no vector is stored; returns True iff an embed call was
    made. Embed failure logs and writes the row anyway — with the OLD hash, so
    the next pass retries (repo COALESCE keeps any existing vector)."""
    if embed_fn is None:
        embed_fn = lambda text: vertex.embed(text, task_type="RETRIEVAL_DOCUMENT")  # noqa: E731
    title = task.get("name") or ""
    notes = task.get("notes") or ""
    chash = content_hash(title, notes)
    state = repo_index.get_state(conn, task["gid"])
    needs_embed = (
        state is None or state["content_hash"] != chash or not state["has_embedding"]
    )
    embedding = None
    if needs_embed:
        try:
            embedding = embed_fn(f"{title}\n{notes}")
        except Exception:
            logger.exception("embed failed for gid=%s — row stored without vector", task["gid"])
            if state is not None:
                chash = state["content_hash"]  # keep mismatch → retried next pass
    repo_index.upsert(
        conn,
        task_gid=task["gid"],
        title=title,
        notes=notes,
        project=_project_name(task),
        completed=bool(task.get("completed")),
        due_on=task.get("due_on"),
        permalink_url=task.get("permalink_url"),
        content_hash=chash,
        embedding=embedding,
    )
    return embedding is not None


def refresh(task_gid: str) -> None:
    """Fetch the task from Asana and re-index it. Best-effort: any failure
    (Asana, DB, Vertex) logs and returns — indexing never crashes a caller."""
    try:
        task = asana.get_task_detail(task_gid)
        if task is None:
            return
        with get_conn() as conn:
            index_task_dict(conn, task)
    except Exception:
        logger.exception("task_index refresh failed for gid=%s", task_gid)
        otel.errors.add(1, {"handler": "task_index"})

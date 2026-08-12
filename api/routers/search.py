import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import clients.asana as asana
import clients.vertex as vertex
from api.auth import verify_token
from api.errors import translate_asana_errors
from clients.db import get_conn
from repo import task_index as repo_index
from services import task_search

logger = logging.getLogger(__name__)

router = APIRouter()


class SearchRequest(BaseModel):
    query: str = ""
    project: str | None = None  # name or GID; None = whole workspace
    completed: bool | None = False  # default: open tasks only; null = both
    due_before: str | None = None  # YYYY-MM-DD, inclusive
    due_after: str | None = None  # YYYY-MM-DD, inclusive
    limit: int = Field(default=25, ge=1, le=100)
    semantic: bool = False  # natural-language nearest-neighbor ranking


class SearchResult(BaseModel):
    task_gid: str
    name: str
    project: str | None = None
    section: str | None = None
    due_on: str | None = None
    completed: bool = False
    permalink_url: str | None = None
    snippet: str | None = None
    summary: str | None = None  # one-line gist of the description, for listings
    message_id: str | None = None
    category: str | None = None
    importance: str | None = None
    parent: str | None = None  # parent task name; set only for subtask hits
    score: float | None = None  # cosine similarity (−1..1, in practice ~0–1); semantic hits only


class SearchResponse(BaseModel):
    results: list[SearchResult]
    semantic: bool = False  # true iff the semantic path served this response


def email_context(task_gids: list[str]) -> dict[str, dict]:
    """Best-effort email metadata from the tasks DB — {} on any failure
    (Asana is the source of truth; the DB only decorates)."""
    try:
        from clients.db import get_conn
        from repo import tasks as tasks_repo

        with get_conn() as conn:
            return tasks_repo.email_context_by_gids(conn, task_gids)
    except Exception:
        logger.exception("email-context lookup failed — results lack email metadata")
        return {}


def membership(task: dict) -> tuple[str | None, str | None]:
    """(project_name, section_name) from the task's first membership."""
    ms = task.get("memberships") or []
    if not ms:
        return None, None
    return (
        (ms[0].get("project") or {}).get("name"),
        (ms[0].get("section") or {}).get("name"),
    )


def _to_result(
    t: dict, *, query: str, email: dict, parent_name: str | None, score: float | None = None
) -> SearchResult:
    project_name, section_name = membership(t)
    return SearchResult(
        task_gid=t["gid"],
        name=t.get("name") or "",
        project=project_name,
        section=section_name,
        due_on=t.get("due_on"),
        completed=bool(t.get("completed")),
        permalink_url=t.get("permalink_url"),
        snippet=task_search.snippet(t.get("notes"), query),
        summary=task_search.summary(t.get("notes")),
        message_id=email.get("message_id"),
        category=email.get("category"),
        importance=email.get("importance"),
        parent=parent_name or (t.get("parent") or {}).get("name"),
        score=score,
    )


def _semantic_search(body: SearchRequest) -> SearchResponse | None:
    """None = semantic ranking unavailable (embed or DB failure) — the caller
    falls back to the substring path. Asana errors and bad requests raise."""
    project_name = None
    if body.project:
        with translate_asana_errors():
            projects = asana.list_projects()
        project = task_search.resolve_project(projects, body.project)
        if project is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"unknown project: {body.project}",
                    "known_projects": [p["name"] for p in projects],
                },
            )
        project_name = project["name"]

    try:
        qvec = vertex.embed(body.query, task_type="RETRIEVAL_QUERY")
        with get_conn() as conn:
            rows = repo_index.semantic_candidates(
                conn,
                query_embedding=qvec,
                completed=body.completed,
                due_before=body.due_before,
                due_after=body.due_after,
                project=project_name,
                limit=body.limit,
            )
    except Exception:
        logger.exception("semantic ranking failed")
        return None

    scores = {r["task_gid"]: r["score"] for r in rows}  # insertion order = rank
    with translate_asana_errors():
        with ThreadPoolExecutor(max_workers=8) as pool:
            details = list(pool.map(asana.get_task_detail, list(scores)))
    tasks = [t for t in details if t is not None]  # deleted since indexing → drop
    ctx = email_context([t["gid"] for t in tasks])
    results = [
        _to_result(
            t,
            query=body.query,
            email=ctx.get(t["gid"], {}),
            parent_name=None,
            score=round(scores[t["gid"]], 4),
        )
        for t in tasks
    ]
    return SearchResponse(results=results, semantic=True)


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest, _: None = Depends(verify_token)) -> SearchResponse:
    if body.semantic:
        if not body.query.strip():
            raise HTTPException(status_code=400, detail="semantic search requires a query")
        semantic_response = _semantic_search(body)
        if semantic_response is not None:
            return semantic_response
        logger.warning("semantic search unavailable — serving substring results")

    only_open = body.completed is False
    parent_names: dict[str, str] = {}
    with translate_asana_errors():
        projects = asana.list_projects()
        if body.project:
            project = task_search.resolve_project(projects, body.project)
            if project is None:
                raise HTTPException(
                    status_code=400,
                    detail={
                        "error": f"unknown project: {body.project}",
                        "known_projects": [p["name"] for p in projects],
                    },
                )
            raw = asana.list_project_tasks(project["gid"], only_open=only_open)
        else:
            with ThreadPoolExecutor(max_workers=8) as pool:
                per_project = list(
                    pool.map(
                        lambda p: asana.list_project_tasks(p["gid"], only_open=only_open),
                        projects,
                    )
                )
            raw = [t for batch in per_project for t in batch] + asana.list_my_tasks(
                only_open=only_open
            )

        # Subtask fan-out (one level): subtasks live under their parent, not in
        # project listings, so fetch them for any swept task that has some.
        # Completed subtasks come back too; filter_tasks applies the completed
        # filter downstream.
        with_subs = [t for t in raw if t.get("num_subtasks")]
        if with_subs:
            with ThreadPoolExecutor(max_workers=8) as pool:
                batches = list(pool.map(lambda t: asana.get_subtasks(t["gid"]), with_subs))
            for parent_task, batch in zip(with_subs, batches):
                for sub in batch:
                    parent_names.setdefault(sub["gid"], parent_task.get("name") or "")
                raw.extend(batch)

    filtered = task_search.filter_tasks(
        raw,
        query=body.query,
        completed=body.completed,
        due_before=body.due_before,
        due_after=body.due_after,
    )[: body.limit]

    ctx = email_context([t["gid"] for t in filtered])
    results = [
        _to_result(
            t,
            query=body.query,
            email=ctx.get(t["gid"], {}),
            parent_name=parent_names.get(t["gid"]),
        )
        for t in filtered
    ]
    return SearchResponse(results=results, semantic=False)

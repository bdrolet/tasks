import logging
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import clients.asana as asana
from api.auth import verify_token
from api.errors import translate_asana_errors
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


class SearchResult(BaseModel):
    task_gid: str
    name: str
    project: str | None = None
    section: str | None = None
    due_on: str | None = None
    completed: bool = False
    permalink_url: str | None = None
    snippet: str | None = None
    message_id: str | None = None
    category: str | None = None
    importance: str | None = None


class SearchResponse(BaseModel):
    results: list[SearchResult]


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


@router.post("/search", response_model=SearchResponse)
def search(body: SearchRequest, _: None = Depends(verify_token)) -> SearchResponse:
    only_open = body.completed is False
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

    filtered = task_search.filter_tasks(
        raw,
        query=body.query,
        completed=body.completed,
        due_before=body.due_before,
        due_after=body.due_after,
    )[: body.limit]

    ctx = email_context([t["gid"] for t in filtered])
    results = []
    for t in filtered:
        project_name, section_name = membership(t)
        email = ctx.get(t["gid"], {})
        results.append(
            SearchResult(
                task_gid=t["gid"],
                name=t.get("name") or "",
                project=project_name,
                section=section_name,
                due_on=t.get("due_on"),
                completed=bool(t.get("completed")),
                permalink_url=t.get("permalink_url"),
                snippet=task_search.snippet(t.get("notes"), body.query),
                message_id=email.get("message_id"),
                category=email.get("category"),
                importance=email.get("importance"),
            )
        )
    return SearchResponse(results=results)

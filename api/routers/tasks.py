import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import clients.asana as asana
from api.auth import verify_token
from api.errors import translate_asana_errors
from api.routers.search import email_context, membership
from models.task_content import TaskContent
from services import tags as tags_service
from services import task_search
from services.task_content import render_html_notes

router = APIRouter()


class Comment(BaseModel):
    gid: str
    text: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    is_editable: bool | None = None


class TaskDetail(BaseModel):
    task_gid: str
    name: str
    notes: str | None = None
    html_notes: str | None = None
    completed: bool = False
    due_on: str | None = None
    due_at: str | None = None
    created_at: str | None = None
    modified_at: str | None = None
    permalink_url: str | None = None
    project: str | None = None
    section: str | None = None
    tags: list[str] = []
    assignee: str | None = None
    comments: list[Comment] = []
    message_id: str | None = None
    category: str | None = None
    importance: str | None = None


class CreateTaskRequest(BaseModel):
    name: str = Field(min_length=1)
    priority: str | None = None  # "P0".."P3" — prefixes the title
    context: str | None = None
    key_points: list[str] = []
    links: list[tuple[str, str]] = []  # (url, label)
    action_items: list[tuple[str, str]] = []  # (label, url)
    project: str | None = None  # name or GID; default = configured tasks project
    section: str | None = None  # name or GID within the project
    due_on: str | None = None
    due_at: str | None = None
    tags: list[str] = []
    assignee: str | None = None  # "me", email, or GID — passed through to Asana


class CreatedTaskResponse(BaseModel):
    task_gid: str
    permalink_url: str


class UpdateTaskRequest(BaseModel):
    name: str | None = None
    priority: str | None = None  # requires name in the same request
    context: str | None = None
    key_points: list[str] = []
    links: list[tuple[str, str]] = []
    action_items: list[tuple[str, str]] = []
    completed: bool | None = None
    due_on: str | None = None  # explicit null clears
    due_at: str | None = None  # explicit null clears
    section: str | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    assignee: str | None = None  # explicit null unassigns


def wrap_html_body(html: str) -> str:
    """Asana html_notes/html_text require a <body> root element."""
    return html if html.lstrip().startswith("<body>") else f"<body>{html}</body>"


_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}


def _title(name: str, priority: str | None) -> str:
    if priority is None:
        return name
    if priority not in _VALID_PRIORITIES:
        raise HTTPException(status_code=400, detail=f"invalid priority: {priority}")
    return f"[{priority}] {name}"


def _content_fields_set(body) -> bool:
    """True if any description-content field was provided in the request."""
    return bool(body.model_fields_set & {"context", "key_points", "links", "action_items"})


def _render_content(body) -> str:
    return render_html_notes(
        TaskContent(
            context=body.context,
            key_points=body.key_points,
            links=[tuple(pair) for pair in body.links],
            action_items=[tuple(pair) for pair in body.action_items],
        )
    )


def resolve_section(project_gid: str, ref: str) -> str:
    """Section ref (name or GID) → GID; 400 with candidates when unknown."""
    sections = asana.get_sections(project_gid)
    for s in sections:
        if s["gid"] == ref or s["name"].casefold() == ref.casefold():
            return s["gid"]
    raise HTTPException(
        status_code=400,
        detail={
            "error": f"unknown section: {ref}",
            "known_sections": [s["name"] for s in sections],
        },
    )


def _resolve_project_gid(ref: str | None) -> str:
    if ref is None:
        default = os.environ.get("ASANA_PROJECT_ID")
        if not default:
            raise HTTPException(
                status_code=400, detail="no project given and no default configured"
            )
        return default
    projects = asana.list_projects()
    project = task_search.resolve_project(projects, ref)
    if project is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"unknown project: {ref}",
                "known_projects": [p["name"] for p in projects],
            },
        )
    return project["gid"]


@router.get("/tasks/{gid}", response_model=TaskDetail)
def get_task(gid: str, _: None = Depends(verify_token)) -> TaskDetail:
    with translate_asana_errors():
        task = asana.get_task_detail(gid)
        if task is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {gid}")
        stories = asana.get_stories(gid)

    project_name, section_name = membership(task)
    email = email_context([gid]).get(gid, {})
    comments = [
        Comment(
            gid=s["gid"],
            text=s.get("text"),
            created_by=(s.get("created_by") or {}).get("name"),
            created_at=s.get("created_at"),
            is_editable=s.get("is_editable"),
        )
        for s in stories
        if s.get("type") == "comment"
    ]
    return TaskDetail(
        task_gid=task["gid"],
        name=task.get("name") or "",
        notes=task.get("notes"),
        html_notes=task.get("html_notes"),
        completed=bool(task.get("completed")),
        due_on=task.get("due_on"),
        due_at=task.get("due_at"),
        created_at=task.get("created_at"),
        modified_at=task.get("modified_at"),
        permalink_url=task.get("permalink_url"),
        project=project_name,
        section=section_name,
        tags=[t["name"] for t in task.get("tags") or []],
        assignee=(task.get("assignee") or {}).get("name"),
        comments=comments,
        message_id=email.get("message_id"),
        category=email.get("category"),
        importance=email.get("importance"),
    )


@router.post("/tasks", response_model=CreatedTaskResponse, status_code=201)
def create_task(body: CreateTaskRequest, _: None = Depends(verify_token)) -> CreatedTaskResponse:
    title = _title(body.name, body.priority)  # validates priority before any Asana I/O
    with translate_asana_errors():
        project_gid = _resolve_project_gid(body.project)

        fields: dict = {
            "name": title,
            "projects": [project_gid],
            "html_notes": _render_content(body),
        }
        if body.due_on is not None:
            fields["due_on"] = body.due_on
        if body.due_at is not None:
            fields["due_at"] = body.due_at
        if body.assignee is not None:
            fields["assignee"] = body.assignee
        if body.tags:
            gids = tags_service.resolve_gids(body.tags)
            if gids:
                fields["tags"] = gids

        section_gid = resolve_section(project_gid, body.section) if body.section else None
        created = asana.create_task_from_fields(fields)
        if section_gid:
            asana.add_task_to_section(created.gid, section_gid)

    return CreatedTaskResponse(task_gid=created.gid, permalink_url=created.permalink_url)


@router.patch("/tasks/{gid}")
def patch_task(gid: str, body: UpdateTaskRequest, _: None = Depends(verify_token)) -> dict:
    if body.priority is not None and body.name is None:
        raise HTTPException(status_code=400, detail="priority requires name in the same request")

    with translate_asana_errors():
        task = asana.get_task_detail(gid)
        if task is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {gid}")

        fields: dict = {}
        if body.name is not None:
            fields["name"] = _title(body.name, body.priority)
        if _content_fields_set(body):
            fields["html_notes"] = _render_content(body)
        if body.completed is not None:
            fields["completed"] = body.completed
        # Nullable fields: explicit null in the request body clears the value.
        for field in ("due_on", "due_at", "assignee"):
            if field in body.model_fields_set:
                fields[field] = getattr(body, field)
        if fields:
            asana.update_task(gid, fields)

        if body.section:
            memberships = task.get("memberships") or []
            project_gid = (memberships[0].get("project") or {}).get("gid") if memberships else None
            if not project_gid:
                raise HTTPException(
                    status_code=400, detail="task is in no project — cannot move sections"
                )
            asana.add_task_to_section(gid, resolve_section(project_gid, body.section))

        if body.add_tags:
            for tag_gid in tags_service.resolve_gids(body.add_tags):
                asana.add_tag(gid, tag_gid)
        if body.remove_tags:
            current: dict[str, str] = {
                (t.get("name") or "").casefold(): t["gid"] for t in task.get("tags") or []
            }
            for name in body.remove_tags:
                tag_gid_opt = current.get(name.casefold())
                if tag_gid_opt:  # unknown removes are ignored (idempotent)
                    asana.remove_tag(gid, tag_gid_opt)

    return {"status": "updated", "task_gid": gid}

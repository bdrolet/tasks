"""All Asana REST calls. Migrated from inbox clients/asana.py; create_task now
takes the email_classified event payload plus enrichment results (key points,
links, due date) computed by handlers/task_create.py."""

import logging
import os
import time
from datetime import date

import httpx

import clients.otel as otel
from models.events import CreatedTask, EmailClassifiedEvent

logger = logging.getLogger(__name__)

ASANA_API_KEY = os.environ.get("ASANA_API_KEY", "")
ASANA_PROJECT_ID = os.environ.get("ASANA_PROJECT_ID", "")
_BASE = "https://app.asana.com/api/1.0"

SEARCH_OPT_FIELDS = (
    "name,notes,due_on,completed,permalink_url,"
    "memberships.project.gid,memberships.project.name,memberships.section.name"
)
DETAIL_OPT_FIELDS = (
    "name,notes,html_notes,completed,due_on,due_at,created_at,modified_at,"
    "permalink_url,tags.gid,tags.name,assignee.gid,assignee.name,"
    "memberships.project.gid,memberships.project.name,"
    "memberships.section.gid,memberships.section.name"
)
STORY_OPT_FIELDS = "type,text,created_by.name,created_at,is_editable"

_workspace_gid: str | None = None


def _request(method: str, path: str, *, operation: str, **kwargs) -> httpx.Response:
    """Single choke point for Asana calls — records asana.api.duration per operation."""
    t0 = time.monotonic()
    try:
        return httpx.request(
            method,
            f"{_BASE}{path}",
            headers={"Authorization": f"Bearer {ASANA_API_KEY}"},
            timeout=10,
            **kwargs,
        )
    finally:
        otel.api_duration.record((time.monotonic() - t0) * 1000, {"operation": operation})


def get_workspace_gid() -> str:
    global _workspace_gid
    if not _workspace_gid:
        resp = _request(
            "GET",
            f"/projects/{ASANA_PROJECT_ID}",
            operation="get_workspace",
            params={"opt_fields": "workspace"},
        )
        resp.raise_for_status()
        _workspace_gid = resp.json()["data"]["workspace"]["gid"]
    return _workspace_gid


def find_tag(name: str, workspace_gid: str) -> str | None:
    """Search workspace tags by name via typeahead; return GID or None."""
    resp = _request(
        "GET",
        f"/workspaces/{workspace_gid}/typeahead",
        operation="find_tag",
        params={"resource_type": "tag", "query": name},
    )
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("name", "").casefold() == name.casefold():
            return item["gid"]
    return None


def create_tag(name: str, workspace_gid: str) -> str:
    """Create a new tag in the workspace; return its GID."""
    resp = _request(
        "POST",
        "/tags",
        operation="create_tag",
        json={"data": {"name": name, "workspace": workspace_gid}},
    )
    resp.raise_for_status()
    return resp.json()["data"]["gid"]


def create_task(
    event: EmailClassifiedEvent,
    *,
    tag_gids: list[str] | None = None,
    due_date: str | None = None,
    html_notes: str = "",
    title: str | None = None,
) -> CreatedTask | None:
    """Create an Asana task from an email_classified event. The description is
    pre-rendered by the caller (handlers/task_create.py via services/task_content).
    Returns None if Asana is not configured or a task for this message_id already
    exists."""
    if not ASANA_API_KEY or not ASANA_PROJECT_ID:
        return None

    payload: dict = {
        # Enriched title comes from the caller; this is only the last-resort
        # fallback. Standard: "Title" section of docs/task-content-standard.md
        # (authoritative — doc wins).
        "name": title or f"[{event['importance']}] {event['subject'] or '(no subject)'}",
        "html_notes": html_notes,
        "projects": [ASANA_PROJECT_ID],
        "external": {"gid": event["message_id"], "data": "inbox"},
    }
    if due_date:
        payload["due_on"] = due_date
    if tag_gids:
        payload["tags"] = tag_gids

    resp = _request(
        "POST",
        "/tasks",
        operation="create_task",
        params={"opt_fields": "gid,permalink_url"},
        json={"data": payload},
    )
    if resp.status_code == 400:
        errs = resp.json().get("errors", [])
        if any("already assigned" in e.get("message", "").lower() for e in errs):
            logger.warning(
                "Asana task for message_id=%s already exists (duplicate external.gid) — skipping",
                event["message_id"],
            )
            return None
    resp.raise_for_status()
    data = resp.json()["data"]
    return CreatedTask(gid=data["gid"], permalink_url=data["permalink_url"])


def add_task_to_section(task_gid: str, section_gid: str) -> None:
    resp = _request(
        "POST",
        f"/sections/{section_gid}/addTask",
        operation="add_task_to_section",
        json={"data": {"task": task_gid}},
    )
    resp.raise_for_status()


def get_sections(project_gid: str | None = None) -> list[dict]:
    resp = _request(
        "GET",
        f"/projects/{project_gid or ASANA_PROJECT_ID}/sections",
        operation="get_sections",
        params={"opt_fields": "name"},
    )
    resp.raise_for_status()
    return [{"gid": s["gid"], "name": s["name"]} for s in resp.json()["data"]]


def get_task(task_gid: str) -> dict:
    resp = _request(
        "GET",
        f"/tasks/{task_gid}",
        operation="get_task",
        params={
            "opt_fields": "completed,name,memberships.section.gid,"
            "memberships.section.name,memberships.project.gid"
        },
    )
    resp.raise_for_status()
    return resp.json()["data"]


def current_section(task: dict) -> dict | None:
    """Return this project's {'gid', 'name'} section membership, or None."""
    for m in task.get("memberships", []):
        if (m.get("project") or {}).get("gid") == ASANA_PROJECT_ID:
            section = m.get("section") or {}
            if section.get("gid"):
                return {"gid": section["gid"], "name": section.get("name", "")}
    return None


def complete_task(task_gid: str) -> None:
    resp = _request(
        "PUT", f"/tasks/{task_gid}", operation="complete_task", json={"data": {"completed": True}}
    )
    resp.raise_for_status()


def add_tag(task_gid: str, tag_gid: str) -> None:
    resp = _request(
        "POST", f"/tasks/{task_gid}/addTag", operation="add_tag", json={"data": {"tag": tag_gid}}
    )
    resp.raise_for_status()


def find_task_by_external(external_gid: str) -> str | None:
    """Look up a task by the external.gid it was created with (message_id)."""
    resp = _request(
        "GET",
        f"/tasks/external:{external_gid}",
        operation="find_task_by_external",
        params={"opt_fields": "gid"},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["data"]["gid"]


def get_incomplete_tasks_past_due(project_gid: str | None = None) -> list[dict]:
    """Incomplete tasks in the project whose due_on is before today."""
    resp = _request(
        "GET",
        "/tasks",
        operation="get_incomplete_tasks_past_due",
        params={
            "project": project_gid or ASANA_PROJECT_ID,
            "completed_since": "now",
            "opt_fields": "name,due_on,memberships.section.gid,"
            "memberships.section.name,memberships.project.gid",
        },
    )
    resp.raise_for_status()
    today = date.today().isoformat()
    return [t for t in resp.json()["data"] if t.get("due_on") and t["due_on"] < today]


def _paginate(path: str, params: dict, *, operation: str) -> list[dict]:
    """Follow Asana offset pagination until exhausted (page size 100)."""
    items: list[dict] = []
    offset: str | None = None
    while True:
        page_params = dict(params, limit=100)
        if offset:
            page_params["offset"] = offset
        resp = _request("GET", path, operation=operation, params=page_params)
        resp.raise_for_status()
        body = resp.json()
        items.extend(body["data"])
        offset = (body.get("next_page") or {}).get("offset")
        if not offset:
            return items


def list_projects() -> list[dict]:
    """Unarchived projects in the workspace: [{gid, name}]."""
    return _paginate(
        "/projects",
        {"workspace": get_workspace_gid(), "archived": "false", "opt_fields": "name"},
        operation="list_projects",
    )


def list_tags() -> list[dict]:
    """All tags in the workspace: [{gid, name}]."""
    return _paginate(
        f"/workspaces/{get_workspace_gid()}/tags",
        {"opt_fields": "name"},
        operation="list_tags",
    )


def list_project_tasks(project_gid: str, *, only_open: bool = False) -> list[dict]:
    params: dict = {"project": project_gid, "opt_fields": SEARCH_OPT_FIELDS}
    if only_open:
        params["completed_since"] = "now"
    return _paginate("/tasks", params, operation="list_project_tasks")


def list_my_tasks(*, only_open: bool = False) -> list[dict]:
    """Workspace tasks assigned to the token's user — catches My-Tasks items
    that are in no project. Overlaps with project listings; callers de-dupe."""
    params: dict = {
        "assignee": "me",
        "workspace": get_workspace_gid(),
        "opt_fields": SEARCH_OPT_FIELDS,
    }
    if only_open:
        params["completed_since"] = "now"
    return _paginate("/tasks", params, operation="list_my_tasks")


def get_task_detail(task_gid: str) -> dict | None:
    resp = _request(
        "GET",
        f"/tasks/{task_gid}",
        operation="get_task_detail",
        params={"opt_fields": DETAIL_OPT_FIELDS},
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()["data"]


def get_stories(task_gid: str) -> list[dict]:
    return _paginate(
        f"/tasks/{task_gid}/stories",
        {"opt_fields": STORY_OPT_FIELDS},
        operation="get_stories",
    )


def create_task_from_fields(fields: dict) -> CreatedTask:
    """Create a task from raw Asana fields (ad-hoc / API-driven creation —
    contrast create_task, which builds fields from an email event)."""
    resp = _request(
        "POST",
        "/tasks",
        operation="create_task_from_fields",
        params={"opt_fields": "gid,permalink_url"},
        json={"data": fields},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    return CreatedTask(gid=data["gid"], permalink_url=data["permalink_url"])


def update_task(task_gid: str, fields: dict) -> None:
    resp = _request("PUT", f"/tasks/{task_gid}", operation="update_task", json={"data": fields})
    resp.raise_for_status()


def remove_tag(task_gid: str, tag_gid: str) -> None:
    resp = _request(
        "POST",
        f"/tasks/{task_gid}/removeTag",
        operation="remove_tag",
        json={"data": {"tag": tag_gid}},
    )
    resp.raise_for_status()


def _story_data(text: str | None, html_text: str | None) -> dict:
    return {"text": text} if text is not None else {"html_text": html_text}


def create_story(task_gid: str, *, text: str | None = None, html_text: str | None = None) -> dict:
    resp = _request(
        "POST",
        f"/tasks/{task_gid}/stories",
        operation="create_story",
        params={"opt_fields": "gid,text,created_at"},
        json={"data": _story_data(text, html_text)},
    )
    resp.raise_for_status()
    return resp.json()["data"]


def update_story(story_gid: str, *, text: str | None = None, html_text: str | None = None) -> None:
    resp = _request(
        "PUT",
        f"/stories/{story_gid}",
        operation="update_story",
        json={"data": _story_data(text, html_text)},
    )
    resp.raise_for_status()


def delete_story(story_gid: str) -> None:
    resp = _request("DELETE", f"/stories/{story_gid}", operation="delete_story")
    resp.raise_for_status()

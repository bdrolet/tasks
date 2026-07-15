"""All Asana REST calls. Migrated from inbox clients/asana.py; create_task now
takes the email_classified event payload plus enrichment results (key points,
links, due date) computed by handlers/task_create.py."""

import logging
import os
import time
import urllib.parse
from datetime import date

import httpx

import clients.otel as otel
from models.events import CreatedTask, EmailClassifiedEvent

logger = logging.getLogger(__name__)

ASANA_API_KEY = os.environ.get("ASANA_API_KEY", "")
ASANA_PROJECT_ID = os.environ.get("ASANA_PROJECT_ID", "")
_BASE = "https://app.asana.com/api/1.0"

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


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _html_notes(
    event: EmailClassifiedEvent,
    key_points: list[str] | None,
    relevant_links: list[list[str]] | None,
) -> str:
    message_id = event["message_id"]
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    label_token = os.environ.get("WEBHOOK_LABEL_TOKEN", "")

    def action_url(label: str, source: str) -> str:
        params = f"id={message_id}&label={label}&source={source}"
        if label_token:
            params += f"&token={urllib.parse.quote(label_token, safe='')}"
        return f"{webhook_url}/label?{params}"

    web_link = event.get("web_link")
    outlook_link = f'<a href="{_esc(web_link)}">Open in Outlook</a>\n' if web_link else ""

    if event["category"] == "respond":
        confirm_label, confirm_text = "respond", "Confirmed respond"
        alt_label, alt_text = "review", "Review instead"
    else:
        confirm_label, confirm_text = "review", "Confirmed review"
        alt_label, alt_text = "respond", "Respond instead"

    action_items = (
        f'<li><a href="{_esc(action_url(confirm_label, "human_confirmation"))}">{confirm_text}</a></li>'
        f'<li><a href="{_esc(action_url(alt_label, "human_correction"))}">{alt_text}</a></li>'
        f'<li><a href="{_esc(action_url("reference", "human_correction"))}">Reference</a></li>'
        f'<li><a href="{_esc(action_url("ignore", "human_correction"))}">Ignore</a></li>'
    )

    draft_link = event.get("draft_link")
    draft_item = (
        f'<li><a href="{_esc(draft_link)}">Open draft reply in Outlook</a></li>'
        if draft_link
        else ""
    )

    if key_points:
        key_points_html = (
            "<strong>Key points:</strong><ul>"
            + "".join(f"<li>{_esc(p)}</li>" for p in key_points)
            + "</ul>"
        )
    else:
        body = event["body"] or ""
        preview = _esc(body[:500]) + ("..." if len(body) > 500 else "")
        key_points_html = f"<strong>Preview:</strong>\n{preview}\n"

    if relevant_links:
        links_html = (
            "<strong>Links:</strong><ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>' for url, label in relevant_links
            )
            + "</ul>"
        )
    else:
        links_html = ""

    to_item = (
        f"<li><strong>To:</strong> {_esc(', '.join(event['to']))}</li>" if event.get("to") else ""
    )
    cc_item = (
        f"<li><strong>Cc:</strong> {_esc(', '.join(event['cc']))}</li>" if event.get("cc") else ""
    )

    return (
        "<body>"
        "<ul>"
        f"<li><strong>From:</strong> {_esc(event['sender_display'])} ({_esc(event['sender'])})</li>"
        f"{to_item}"
        f"{cc_item}"
        f"<li><strong>Received:</strong> {_esc(event['received_at'])}</li>"
        f"<li><strong>Importance:</strong> {_esc(event['importance'])}</li>"
        f"<li><strong>Tags:</strong> {_esc(', '.join(event['tags']) or 'none')}</li>"
        f"{draft_item}"
        "</ul>"
        f"<strong>AI reasoning:</strong> {_esc(event['reasoning'])}\n"
        f"\n{key_points_html}"
        f"\n{links_html}"
        f"\n{outlook_link}"
        "\n<strong>Actions</strong>"
        f"<ul>{action_items}</ul>"
        "</body>"
    )


def create_task(
    event: EmailClassifiedEvent,
    *,
    tag_gids: list[str] | None = None,
    key_points: list[str] | None = None,
    relevant_links: list[list[str]] | None = None,
    due_date: str | None = None,
) -> CreatedTask | None:
    """Create an Asana task from an email_classified event plus enrichment
    results. Returns None if Asana is not configured or a task for this
    message_id already exists."""
    if not ASANA_API_KEY or not ASANA_PROJECT_ID:
        return None

    payload: dict = {
        "name": f"[{event['importance']}] {event['subject'] or '(no subject)'}",
        "html_notes": _html_notes(event, key_points, relevant_links),
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

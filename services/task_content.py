"""Build and render a task's description content.

render_html_notes turns a TaskContent into the Asana html_notes string —
deterministic code, never an LLM (a scannable standard needs identical output
for identical input). for_email (Task 3) builds the email-derived TaskContent.
"""

import os
import urllib.parse

from models.events import EmailClassifiedEvent
from models.task_content import Source, TaskContent


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_source(source: Source | None) -> str:
    if source is None:
        return "<strong>Source:</strong> Created manually"
    parts = [f"<strong>Source:</strong> {_esc(source.origin)}"]
    if source.rows:
        parts.append(
            "<ul>"
            + "".join(
                f"<li><strong>{_esc(label)}:</strong> {_esc(value)}</li>"
                for label, value in source.rows
            )
            + "</ul>"
        )
    if source.links:
        parts.append(
            "<ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>' for url, label in source.links
            )
            + "</ul>"
        )
    if source.note:
        parts.append(f"<p>{_esc(source.note)}</p>")
    return "".join(parts)


def render_html_notes(content: TaskContent) -> str:
    parts: list[str] = []
    if content.context:
        parts.append(f"<p>{_esc(content.context)}</p>")
    if content.key_points:
        parts.append(
            "<strong>Key points:</strong><ul>"
            + "".join(f"<li>{_esc(p)}</li>" for p in content.key_points)
            + "</ul>"
        )
    if content.links:
        parts.append(
            "<strong>Links:</strong><ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>' for url, label in content.links
            )
            + "</ul>"
        )
    if content.action_items:
        parts.append(
            "<strong>Actions</strong><ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>'
                for label, url in content.action_items
            )
            + "</ul>"
        )
    parts.append(_render_source(content.source))
    return "<body>" + "".join(parts) + "</body>"


def _action_url(message_id: str, label: str, source: str) -> str:
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    label_token = os.environ.get("WEBHOOK_LABEL_TOKEN", "")
    params = f"id={message_id}&label={label}&source={source}"
    if label_token:
        params += f"&token={urllib.parse.quote(label_token, safe='')}"
    return f"{webhook_url}/label?{params}"


def for_email(
    event: EmailClassifiedEvent,
    key_points: list[str],
    relevant_links: list[list[str]],
) -> TaskContent:
    """Build the email-derived TaskContent (substance + provenance footer)."""
    message_id = event["message_id"]

    context = None
    if not key_points:
        body = event["body"] or ""
        context = body[:500] + ("..." if len(body) > 500 else "")

    if event["category"] == "respond":
        confirm_label, confirm_text = "respond", "Confirmed respond"
        alt_label, alt_text = "review", "Review instead"
    else:
        confirm_label, confirm_text = "review", "Confirmed review"
        alt_label, alt_text = "respond", "Respond instead"

    action_items: list[tuple[str, str]] = [
        (confirm_text, _action_url(message_id, confirm_label, "human_confirmation")),
        (alt_text, _action_url(message_id, alt_label, "human_correction")),
        ("Reference", _action_url(message_id, "reference", "human_correction")),
        ("Ignore", _action_url(message_id, "ignore", "human_correction")),
    ]
    draft_link = event.get("draft_link")
    if draft_link:
        action_items.append(("Open draft reply in Outlook", draft_link))

    rows = [
        ("From", f"{event['sender_display']} ({event['sender']})"),
        ("Received", event["received_at"]),
    ]
    links = [(event["web_link"], "Open in Outlook")] if event.get("web_link") else []
    source = Source(origin="Email", rows=rows, links=links, note=event["reasoning"])

    return TaskContent(
        context=context,
        key_points=list(key_points),
        links=[(url, label) for url, label in relevant_links],
        action_items=action_items,
        source=source,
    )

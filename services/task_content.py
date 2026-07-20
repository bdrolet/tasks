"""Build and render a task's description content.

render_html_notes turns a TaskContent into the Asana html_notes string —
deterministic code, never an LLM (a scannable standard needs identical output
for identical input). for_email (Task 3) builds the email-derived TaskContent.
"""

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

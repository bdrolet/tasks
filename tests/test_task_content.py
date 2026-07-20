from models.task_content import Source, TaskContent
from services.task_content import render_html_notes


def test_taskcontent_defaults_are_empty():
    c = TaskContent()
    assert c.context is None
    assert c.key_points == []
    assert c.links == []
    assert c.action_items == []
    assert c.source is None


def test_source_defaults():
    s = Source(origin="Email")
    assert s.origin == "Email"
    assert s.rows == []
    assert s.links == []
    assert s.note is None


def test_render_empty_content_has_manual_source_footer():
    html = render_html_notes(TaskContent())
    assert html.startswith("<body>") and html.endswith("</body>")
    assert "<strong>Source:</strong> Created manually" in html
    # no substance sections when everything is empty
    assert "Key points" not in html
    assert "Links:" not in html
    assert "Actions" not in html


def test_render_sections_in_order_and_escaped():
    html = render_html_notes(
        TaskContent(
            context="why <this> & that",
            key_points=["point <one>"],
            links=[("https://x", "Doc & more")],
            action_items=[("Confirm review", "https://hook/label?x=1&y=2")],
            source=Source(
                origin="Email",
                rows=[("From", "Alice <a@x>")],
                links=[("https://outlook/1", "Open in Outlook")],
                note="Needs review",
            ),
        )
    )
    # escaping
    assert "why &lt;this&gt; &amp; that" in html
    assert "<li>point &lt;one&gt;</li>" in html
    assert '<a href="https://x">Doc &amp; more</a>' in html
    # ordering: context before key points before links before actions before source
    assert (
        html.index("why &lt;this")
        < html.index("point &lt;one")
        < html.index("Doc &amp; more")
        < html.index("Confirm review")
        < html.index("<strong>Source:</strong>")
    )
    assert "<strong>Source:</strong> Email" in html
    assert "<li><strong>From:</strong> Alice &lt;a@x&gt;</li>" in html
    assert '<a href="https://outlook/1">Open in Outlook</a>' in html
    assert "Needs review" in html


def test_render_is_deterministic():
    c = TaskContent(key_points=["a", "b"], source=Source(origin="Email"))
    assert render_html_notes(c) == render_html_notes(c)


from services.task_content import for_email
from tests.test_events import make_email_event


def _action_labels(content):
    return [label for label, _ in content.action_items]


def test_for_email_review_action_items_and_source(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    monkeypatch.setenv("WEBHOOK_LABEL_TOKEN", "tok")
    content = for_email(
        make_email_event(category="review"),
        key_points=["Point one"],
        relevant_links=[["https://x", "Doc"]],
    )
    assert content.key_points == ["Point one"]
    assert content.context is None  # key points present → no preview
    assert content.links == [("https://x", "Doc")]
    assert _action_labels(content) == [
        "Confirmed review",
        "Respond instead",
        "Reference",
        "Ignore",
    ]
    # action URLs carry the message id, chosen label, source, and token
    confirm_url = content.action_items[0][1]
    assert "id=msg-123" in confirm_url and "label=review" in confirm_url
    assert "source=human_confirmation" in confirm_url and "token=tok" in confirm_url
    assert content.source.origin == "Email"
    assert content.source.rows == [
        ("From", "Alice (alice@example.com)"),
        ("Received", "2026-07-15T12:00:00Z"),
    ]
    assert content.source.links == [("https://outlook.example/msg-123", "Open in Outlook")]
    assert content.source.note == "Needs review"


def test_for_email_respond_swaps_confirm_and_alt(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    content = for_email(make_email_event(category="respond"), key_points=["p"], relevant_links=[])
    assert _action_labels(content)[:2] == ["Confirmed respond", "Review instead"]


def test_for_email_preview_fallback_without_key_points(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    content = for_email(make_email_event(body="y" * 900), key_points=[], relevant_links=[])
    assert content.key_points == []
    assert content.context is not None
    assert content.context.startswith("y" * 500)
    assert content.context.endswith("...")


def test_for_email_includes_draft_reply_action(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    content = for_email(
        make_email_event(draft_link="https://outlook/draft"),
        key_points=["p"],
        relevant_links=[],
    )
    assert ("Open draft reply in Outlook", "https://outlook/draft") in content.action_items

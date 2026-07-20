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

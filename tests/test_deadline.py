import clients.claude as claude
from services import deadline, standing_context
from tests.test_events import make_email_event


def test_extract_deadline_returns_date(monkeypatch):
    monkeypatch.setattr(claude, "extract", lambda prompt: "2026-07-31")
    assert deadline.extract_deadline(make_email_event()) == "2026-07-31"


def test_extract_deadline_null(monkeypatch):
    monkeypatch.setattr(claude, "extract", lambda prompt: "null")
    assert deadline.extract_deadline(make_email_event()) is None


def test_deadline_text_past_1000_chars_reaches_prompt(monkeypatch):
    """Regression: a deadline stated past char 1000 must still reach the model.
    The extractor previously truncated the body at 1000 chars while the summary
    read 3000, so deadlines further down the body were silently dropped."""
    captured = {}

    def fake_extract(prompt):
        captured["prompt"] = prompt
        return "null"

    monkeypatch.setattr(claude, "extract", fake_extract)
    body = ("filler boilerplate. " * 80) + "Please reply by 2026-08-15."  # marker ~1600 chars in
    deadline.extract_deadline(make_email_event(body=body))
    assert len(body) > 1000
    assert "2026-08-15" in captured["prompt"]


def _capture_prompt(captured):
    def fake_extract(prompt):
        captured["p"] = prompt
        return "null"

    return fake_extract


def test_calendar_section_reaches_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude, "extract", _capture_prompt(captured))
    monkeypatch.setattr(
        standing_context,
        "section",
        lambda name, **kw: (
            "- SFUSD fall term: 2026-08-17 to 2026-12-18." if name == "Calendar" else ""
        ),
    )
    deadline.extract_deadline(make_email_event())
    p = captured["p"]
    assert "Calendar facts:" in p and "SFUSD fall term" in p
    assert p.index("SFUSD") < p.index("Today is")


def test_empty_calendar_leaves_prompt_unchanged(monkeypatch):
    captured = {}
    monkeypatch.setattr(claude, "extract", _capture_prompt(captured))
    monkeypatch.setattr(standing_context, "section", lambda name, **kw: "")
    deadline.extract_deadline(make_email_event())
    assert captured["p"].startswith("Today is ")
    assert "Calendar facts" not in captured["p"]

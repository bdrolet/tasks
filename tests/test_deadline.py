import clients.claude as claude
from services import deadline
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

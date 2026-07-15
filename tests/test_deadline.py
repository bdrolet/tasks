import clients.claude as claude
from services import deadline
from tests.test_events import make_email_event


def test_extract_deadline_returns_date(monkeypatch):
    monkeypatch.setattr(claude, "extract", lambda prompt: "2026-07-31")
    assert deadline.extract_deadline(make_email_event()) == "2026-07-31"


def test_extract_deadline_null(monkeypatch):
    monkeypatch.setattr(claude, "extract", lambda prompt: "null")
    assert deadline.extract_deadline(make_email_event()) is None

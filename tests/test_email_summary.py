import clients.claude as claude
from services import email_summary
from tests.test_events import make_email_event


def test_generate_parses_key_points_and_extracts_links(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda prompt: '{"key_points": ["Do the thing"]}')
    summary = email_summary.generate(make_email_event())
    assert summary.key_points == ["Do the thing"]
    assert summary.relevant_links == [["https://docs.example/q2", "the Q2 report"]]


def test_generate_strips_markdown_fences(monkeypatch):
    monkeypatch.setattr(
        claude, "summarize", lambda prompt: '```json\n{"key_points": ["A"]}\n```'
    )
    assert email_summary.generate(make_email_event()).key_points == ["A"]


def test_generate_survives_claude_failure(monkeypatch):
    def boom(prompt):
        raise RuntimeError("api down")

    monkeypatch.setattr(claude, "summarize", boom)
    summary = email_summary.generate(make_email_event())
    assert summary.key_points == []
    assert summary.relevant_links == [["https://docs.example/q2", "the Q2 report"]]

import clients.claude as claude
from services import email_summary
from tests.test_events import make_email_event


def test_generate_parses_key_points_and_extracts_links(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda prompt: '{"key_points": ["Do the thing"]}')
    summary = email_summary.generate(make_email_event())
    assert summary.key_points == ["Do the thing"]
    assert summary.relevant_links == [["https://docs.example/q2", "the Q2 report"]]


def test_generate_strips_markdown_fences(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda prompt: '```json\n{"key_points": ["A"]}\n```')
    assert email_summary.generate(make_email_event()).key_points == ["A"]


def test_generate_parses_title(monkeypatch):
    monkeypatch.setattr(
        claude,
        "summarize",
        lambda prompt: '{"key_points": ["A"], "title": "Review Q3 board deck"}',
    )
    assert email_summary.generate(make_email_event()).title == "Review Q3 board deck"


def test_generate_title_missing_is_none(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda prompt: '{"key_points": ["A"]}')
    assert email_summary.generate(make_email_event()).title is None


def test_normalize_title_strips_punctuation_and_whitespace():
    assert email_summary._normalize_title("  Review  the   deck.  ") == "Review the deck"


def test_normalize_title_empty_is_none():
    assert email_summary._normalize_title("   ") is None
    assert email_summary._normalize_title(None) is None


def test_normalize_title_truncates_on_word_boundary():
    long = "Review the extremely detailed quarterly board deck before the meeting happens soon"
    out = email_summary._normalize_title(long)
    assert len(out) <= email_summary.MAX_TITLE_CHARS
    assert not out.endswith(" ")
    assert long.startswith(out)  # truncated, not reworded


def test_normalize_title_strips_leading_priority_tag():
    assert email_summary._normalize_title("[P3] Review the deck") == "Review the deck"


def test_normalize_title_priority_tag_only_is_none():
    assert email_summary._normalize_title("[P3]") is None


def test_generate_survives_claude_failure(monkeypatch):
    def boom(prompt):
        raise RuntimeError("api down")

    monkeypatch.setattr(claude, "summarize", boom)
    summary = email_summary.generate(make_email_event())
    assert summary.key_points == []
    assert summary.title is None
    assert summary.relevant_links == [["https://docs.example/q2", "the Q2 report"]]

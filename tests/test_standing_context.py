from pathlib import Path

import pytest

from services import standing_context

DOC = """# Standing context

## Roles

### Assistant Coach
Ended 2026-08-14.

- admin mail is NOT actionable

## Calendar

- SFUSD fall term: 2026-08-17 to 2026-12-18.
"""


@pytest.fixture
def ctx_file(tmp_path, monkeypatch):
    path = tmp_path / "standing-context.md"
    path.write_text(DOC)
    monkeypatch.setattr(standing_context, "PATH", path)
    standing_context.reset_cache()
    yield path
    standing_context.reset_cache()


def test_section_returns_body_by_heading(ctx_file):
    roles = standing_context.section("Roles")
    assert roles.startswith("### Assistant Coach")
    assert "admin mail is NOT actionable" in roles
    assert "SFUSD" not in roles


def test_section_heading_match_is_case_insensitive(ctx_file):
    assert "SFUSD" in standing_context.section("calendar")


def test_missing_section_is_empty(ctx_file):
    assert standing_context.section("Nope") == ""


def test_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(standing_context, "PATH", tmp_path / "absent.md")
    standing_context.reset_cache()
    assert standing_context.section("Roles") == ""


def test_file_is_read_once_until_reset(ctx_file):
    assert "Assistant Coach" in standing_context.section("Roles")
    ctx_file.write_text("# Standing context\n\n## Roles\n\nChanged.\n")
    assert "Assistant Coach" in standing_context.section("Roles")  # cached
    standing_context.reset_cache()
    assert standing_context.section("Roles") == "Changed."


def test_example_file_documents_the_shipped_format():
    """The real facts live in the private bdrolet/context repo and reach the
    function as a mounted secret, so there is nothing here to parse. The example
    is the documented contract between the two repos — if it stops parsing into
    the sections consumers ask for, the docs have drifted from the loader."""
    standing_context.reset_cache()
    example = Path(__file__).resolve().parent.parent / "context" / "standing-context.example.md"
    text = example.read_text()
    assert standing_context.section("Roles", text=text)
    assert standing_context.section("Calendar", text=text)


def test_no_real_facts_committed():
    """context/ is gitignored apart from the README and the example — a real
    fact file appearing here would be personal data in a public repo."""
    ctx = Path(__file__).resolve().parent.parent / "context"
    stray = {p.name for p in ctx.glob("*.md")} - {
        "README.md",
        "standing-context.example.md",
    }
    assert not stray, f"unexpected files in context/: {sorted(stray)}"

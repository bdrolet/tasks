from datetime import date

import pytest

import clients.asana as asana
from handlers import weekly_review as wr
from models.strategy import Goal, Strategy
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(wr, "get_conn", lambda: Conn())
    monkeypatch.setattr(wr, "today_local", lambda: date(2026, 10, 12))
    monkeypatch.setattr(repo_goals, "load_snapshot", lambda c: Strategy(goals=(Goal(id="consulting", kind="outcome"),), text_hash="h"))
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {})
    monkeypatch.setattr(repo, "list_scores", lambda c: [])
    monkeypatch.setattr(repo_sup, "list_necessity", lambda c, limit=100: [])
    calls = {"created": [], "stories": [], "existing": None}
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: calls["existing"])
    monkeypatch.setattr(asana, "create_task_from_fields", lambda f: (calls["created"].append(f), type("T", (), {"gid": "rv1", "permalink_url": "u"})())[1])
    monkeypatch.setattr(asana, "create_story", lambda gid, text=None, html_text=None: calls["stories"].append((gid, text)))
    monkeypatch.setattr(asana, "add_task_to_section", lambda gid, sec: None)
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj")
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec")
    return calls


def test_run_creates_the_standing_task_once_and_posts_the_review(wired):
    out = wr.run()
    assert out == {"outcome": "posted", "task_gid": "rv1"}
    assert wired["created"][0]["name"] == wr.REVIEW_TASK_NAME
    assert wired["created"][0]["external"] == {"gid": wr.REVIEW_EXTERNAL, "data": "tasks"}
    gid, text = wired["stories"][0]
    assert gid == "rv1" and text.startswith("# Weekly strategy review — 2026-10-12")
    wired["existing"] = "rv1"
    wr.run()
    assert len(wired["created"]) == 1 and len(wired["stories"]) == 2


def test_run_without_strategy_posts_nothing(wired, monkeypatch):
    monkeypatch.setattr(repo_goals, "load_snapshot", lambda c: Strategy.EMPTY)
    assert wr.run() == {"outcome": "no_strategy"} and wired["stories"] == []


def test_run_db_unavailable_is_reported_not_raised(wired, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wr, "get_conn", boom)
    assert wr.run() == {"outcome": "db_unavailable"}

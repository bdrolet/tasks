# tests/test_api_review.py
from datetime import date

import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
import clients.pubsub as ps
from api.main import app
from api.routers import review as review_router
from models.strategy import Goal, Strategy
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import task_index

client = TestClient(app)
AUTH = {"Authorization": "Bearer x"}


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    monkeypatch.setattr(review_router, "get_conn", lambda: Conn())
    monkeypatch.setattr(repo_goals, "load_snapshot", lambda c: Strategy(goals=(Goal(id="consulting", kind="outcome"), Goal(id="finances", kind="area")), text_hash="h"))
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {})
    monkeypatch.setattr(repo, "list_scores", lambda c: [])
    monkeypatch.setattr(repo_sup, "list_necessity", lambda c, limit=100: [])
    monkeypatch.setattr(review_router, "_today", lambda: date(2026, 10, 9))


def test_get_review_shape():
    r = client.get("/review", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert {g["id"] for g in body["goals"]} == {"consulting", "finances"}
    assert body["grooming"] == [] and body["stop_doing"] == {"tasks": [], "suppressed_emails": []}


def test_post_report_validates_goal_and_writes(monkeypatch):
    written = []
    monkeypatch.setattr(repo_goals, "insert_report", lambda c, g, v, ps: (written.append((g, v, ps)), 3)[1])
    r = client.post("/goals/consulting/reports", json={"value": 4200, "period_start": "2026-10-01"}, headers=AUTH)
    assert r.status_code == 201 and r.json() == {"id": 3, "goal_id": "consulting", "value": 4200.0, "period_start": "2026-10-01"}
    assert written == [("consulting", 4200.0, date(2026, 10, 1))]
    assert client.post("/goals/ghost/reports", json={"value": 1}, headers=AUTH).status_code == 404
    assert client.post("/goals/finances/reports", json={"value": 1}, headers=AUTH).status_code == 400  # areas have no lag


def test_post_mute_sets_and_clears(monkeypatch):
    calls = []
    monkeypatch.setattr(repo_goals, "set_mute", lambda c, g, until: calls.append((g, until)))
    assert client.post("/goals/finances/mute", json={"until": "2026-10-20"}, headers=AUTH).status_code == 200
    assert client.post("/goals/finances/mute", json={"until": None}, headers=AUTH).status_code == 200
    assert calls == [("finances", date(2026, 10, 20)), ("finances", None)]
    assert client.post("/goals/consulting/mute", json={"until": "2026-10-20"}, headers=AUTH).status_code == 400


def test_restore_creates_once_and_marks(monkeypatch):
    row = {"message_id": "m1", "category": "review", "importance": "P2", "subject": "Newsletter", "sender": "x@y",
           "reason": "serves nothing", "source": "necessity", "web_link": "https://outlook/m1", "restored_at": None,
           "restored_task_gid": None, "created_at": None}
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: dict(row))
    marked = []
    monkeypatch.setattr(repo_sup, "mark_restored", lambda c, mid, gid: (marked.append((mid, gid)), True)[1])
    created = []
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: None)
    monkeypatch.setattr(asana, "create_task_from_fields", lambda f: (created.append(f), type("T", (), {"gid": "t9", "permalink_url": "u"})())[1])
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj")
    monkeypatch.setattr(review_router.tags_service, "resolve_gids", lambda names: [])
    monkeypatch.setattr(task_index, "refresh", lambda gid: None)
    monkeypatch.setattr(ps, "publish_task_changed", lambda gid, source: None)
    r = client.post("/suppressions/m1/restore", headers=AUTH)
    assert r.status_code == 201 and r.json() == {"task_gid": "t9", "permalink_url": "u", "message_id": "m1"}
    assert created[0]["name"] == "[P2] Newsletter" and created[0]["external"] == {"gid": "m1", "data": "inbox"}
    assert "https://outlook/m1" in created[0]["html_notes"] and marked == [("m1", "t9")]
    # second call: already restored
    row["restored_at"], row["restored_task_gid"] = "2026-10-09", "t9"
    r = client.post("/suppressions/m1/restore", headers=AUTH)
    assert r.status_code == 200 and r.json()["task_gid"] == "t9" and len(created) == 1


def test_restore_unknown_or_non_necessity_is_404(monkeypatch):
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: None)
    assert client.post("/suppressions/zz/restore", headers=AUTH).status_code == 404
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: {"message_id": "m", "source": "agent", "restored_at": None})
    assert client.post("/suppressions/m/restore", headers=AUTH).status_code == 404


def test_restore_escapes_html_in_notes(monkeypatch):
    row = {"message_id": "m2", "importance": "P2", "subject": "S", "sender": "Ann <ann@x.y>",
           "reason": "serves nothing & more", "source": "necessity", "web_link": "https://x/?a=1&b=2",
           "restored_at": None, "restored_task_gid": None}
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: dict(row))
    monkeypatch.setattr(repo_sup, "mark_restored", lambda c, mid, gid: True)
    created = []
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: None)
    monkeypatch.setattr(asana, "create_task_from_fields", lambda f: (created.append(f), type("T", (), {"gid": "t9", "permalink_url": "u"})())[1])
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj")
    monkeypatch.setattr(review_router.tags_service, "resolve_gids", lambda names: [])
    monkeypatch.setattr(task_index, "refresh", lambda gid: None)
    monkeypatch.setattr(ps, "publish_task_changed", lambda gid, source: None)
    assert client.post("/suppressions/m2/restore", headers=AUTH).status_code == 201
    notes = created[0]["html_notes"]
    assert "&lt;ann@x.y&gt;" in notes and "&amp; more" in notes and "a=1&amp;b=2" in notes
    assert "<ann@" not in notes

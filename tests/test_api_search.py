import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
from api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    # email_context degrades to {} when the DB is unreachable; simulate that
    # default so tests don't need Postgres. Individual tests override.
    from api.routers import search as search_router

    monkeypatch.setattr(search_router, "email_context", lambda gids: {})


def _task(gid, name, notes="", project="Inbox", section=None, **kw):
    return {
        "gid": gid,
        "name": name,
        "notes": notes,
        "completed": kw.get("completed", False),
        "due_on": kw.get("due_on"),
        "permalink_url": f"https://app.asana.com/x/{gid}",
        "memberships": [{"project": {"gid": "p1", "name": project}, "section": {"name": section}}],
    }


def test_search_requires_token():
    assert client.post("/search", json={"query": "x"}).status_code == 401


def test_search_workspace_wide(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "list_project_tasks",
        lambda gid, **kw: [_task("t1", "Renew passport", notes="expires soon")],
    )
    monkeypatch.setattr(asana, "list_my_tasks", lambda **kw: [_task("t2", "passport photos")])

    resp = client.post("/search", json={"query": "passport"}, headers=AUTH)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert {r["task_gid"] for r in results} == {"t1", "t2"}
    r1 = next(r for r in results if r["task_gid"] == "t1")
    assert r1["project"] == "Inbox"
    assert r1["snippet"] is None  # match was in the name, not notes


def test_search_project_narrowing_by_name(monkeypatch):
    monkeypatch.setattr(
        asana,
        "list_projects",
        lambda: [{"gid": "p1", "name": "Inbox"}, {"gid": "p2", "name": "Chores"}],
    )
    calls = []

    def fake_list(gid, **kw):
        calls.append(gid)
        return [_task("t1", "Mow lawn", project="Chores")]

    monkeypatch.setattr(asana, "list_project_tasks", fake_list)

    resp = client.post("/search", json={"query": "mow", "project": "chores"}, headers=AUTH)
    assert resp.status_code == 200
    assert calls == ["p2"]


def test_search_unknown_project_400_with_candidates(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    resp = client.post("/search", json={"query": "x", "project": "nope"}, headers=AUTH)
    assert resp.status_code == 400
    assert "Inbox" in str(resp.json()["detail"])


def test_search_decorates_email_context(monkeypatch):
    from api.routers import search as search_router

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    monkeypatch.setattr(asana, "list_project_tasks", lambda gid, **kw: [_task("t1", "[P1] Budget")])
    monkeypatch.setattr(asana, "list_my_tasks", lambda **kw: [])
    monkeypatch.setattr(
        search_router,
        "email_context",
        lambda gids: {"t1": {"message_id": "m1", "category": "respond", "importance": "P1"}},
    )

    resp = client.post("/search", json={"query": "budget"}, headers=AUTH)
    r = resp.json()["results"][0]
    assert r["message_id"] == "m1"
    assert r["category"] == "respond"


def test_search_passes_only_open_true_by_default(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    calls: dict = {}

    def fake_list_project_tasks(gid, **kwargs):
        calls["project_only_open"] = kwargs.get("only_open")
        return []

    def fake_list_my_tasks(**kwargs):
        calls["my_only_open"] = kwargs.get("only_open")
        return []

    monkeypatch.setattr(asana, "list_project_tasks", fake_list_project_tasks)
    monkeypatch.setattr(asana, "list_my_tasks", fake_list_my_tasks)

    resp = client.post("/search", json={"query": "x"}, headers=AUTH)
    assert resp.status_code == 200
    assert calls["project_only_open"] is True
    assert calls["my_only_open"] is True


def test_search_passes_only_open_false_when_completed_null(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    calls: dict = {}

    def fake_list_project_tasks(gid, **kwargs):
        calls["project_only_open"] = kwargs.get("only_open")
        return []

    def fake_list_my_tasks(**kwargs):
        calls["my_only_open"] = kwargs.get("only_open")
        return []

    monkeypatch.setattr(asana, "list_project_tasks", fake_list_project_tasks)
    monkeypatch.setattr(asana, "list_my_tasks", fake_list_my_tasks)

    resp = client.post("/search", json={"query": "x", "completed": None}, headers=AUTH)
    assert resp.status_code == 200
    assert calls["project_only_open"] is False
    assert calls["my_only_open"] is False


def test_search_project_narrowed_passes_only_open(monkeypatch):
    monkeypatch.setattr(
        asana,
        "list_projects",
        lambda: [{"gid": "p1", "name": "Inbox"}, {"gid": "p2", "name": "Chores"}],
    )
    calls: dict = {}

    def fake_list_project_tasks(gid, **kwargs):
        calls["only_open"] = kwargs.get("only_open")
        return []

    monkeypatch.setattr(asana, "list_project_tasks", fake_list_project_tasks)

    resp = client.post("/search", json={"query": "mow", "project": "chores"}, headers=AUTH)
    assert resp.status_code == 200
    assert calls["only_open"] is True


def test_search_limit(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "list_project_tasks",
        lambda gid, **kw: [_task(f"t{i}", f"item {i}") for i in range(30)],
    )
    monkeypatch.setattr(asana, "list_my_tasks", lambda **kw: [])
    resp = client.post("/search", json={"query": "item", "limit": 5}, headers=AUTH)
    assert len(resp.json()["results"]) == 5

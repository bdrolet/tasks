import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
from api.main import app
from models.events import CreatedTask

client = TestClient(app)
AUTH = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-email")


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    from api.routers import tasks as tasks_router

    monkeypatch.setattr(tasks_router, "email_context", lambda gids: {})


DETAIL = {
    "gid": "t1",
    "name": "[P1] Budget review",
    "notes": "plain body",
    "html_notes": "<body>plain body</body>",
    "completed": False,
    "due_on": "2026-07-20",
    "due_at": None,
    "created_at": "2026-07-10T00:00:00Z",
    "modified_at": "2026-07-15T00:00:00Z",
    "permalink_url": "https://app.asana.com/x/t1",
    "tags": [{"gid": "tag1", "name": "finance"}],
    "assignee": {"gid": "u1", "name": "Ben"},
    "memberships": [
        {"project": {"gid": "p-email", "name": "Inbox"}, "section": {"gid": "s1", "name": "Review"}}
    ],
}

STORIES = [
    {"gid": "s-sys", "type": "system", "text": "added to Inbox", "created_at": "…"},
    {
        "gid": "s-c1",
        "type": "comment",
        "text": "looks fine",
        "created_by": {"name": "Ben"},
        "created_at": "2026-07-11T00:00:00Z",
        "is_editable": True,
    },
]


def test_get_task_detail_with_comments(monkeypatch):
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: dict(DETAIL))
    monkeypatch.setattr(asana, "get_stories", lambda gid: list(STORIES))

    resp = client.get("/tasks/t1", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "[P1] Budget review"
    assert body["project"] == "Inbox"
    assert body["section"] == "Review"
    assert body["tags"] == ["finance"]
    assert body["assignee"] == "Ben"
    # system stories filtered out
    assert [c["gid"] for c in body["comments"]] == ["s-c1"]
    assert body["comments"][0]["created_by"] == "Ben"


def test_get_task_404(monkeypatch):
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: None)
    assert client.get("/tasks/nope", headers=AUTH).status_code == 404


def test_create_task_defaults_to_email_project(monkeypatch):
    captured = {}

    def fake_create(fields):
        captured.update(fields)
        return CreatedTask(gid="t9", permalink_url="https://a/t9")

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)

    resp = client.post(
        "/tasks",
        json={"name": "Buy milk", "description": "2%", "due_on": "2026-07-20"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert resp.json() == {"task_gid": "t9", "permalink_url": "https://a/t9"}
    assert captured["projects"] == ["p-email"]
    assert captured["notes"] == "2%"
    assert captured["due_on"] == "2026-07-20"


def test_create_task_html_description_wrapped(monkeypatch):
    captured = {}

    def fake_create(fields):
        captured.update(fields)
        return CreatedTask(gid="t9", permalink_url="https://a/t9")

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)

    client.post("/tasks", json={"name": "X", "html_description": "<b>hi</b>"}, headers=AUTH)
    assert captured["html_notes"] == "<body><b>hi</b></body>"


def test_create_task_rejects_both_descriptions():
    resp = client.post(
        "/tasks",
        json={"name": "X", "description": "a", "html_description": "b"},
        headers=AUTH,
    )
    assert resp.status_code == 400


def test_create_task_with_section_and_tags(monkeypatch):
    from api.routers import tasks as tasks_router

    moved = {}
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p2", "name": "Chores"}])
    monkeypatch.setattr(asana, "get_sections", lambda gid: [{"gid": "sec1", "name": "This week"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    monkeypatch.setattr(
        asana,
        "add_task_to_section",
        lambda task_gid, section_gid: moved.update(t=task_gid, s=section_gid),
    )
    monkeypatch.setattr(tasks_router.tags_service, "resolve_gids", lambda names: ["tg1"])

    resp = client.post(
        "/tasks",
        json={"name": "X", "project": "Chores", "section": "this week", "tags": ["home"]},
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert moved == {"t": "t9", "s": "sec1"}


def test_create_task_unknown_section_400(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p2", "name": "Chores"}])
    monkeypatch.setattr(asana, "get_sections", lambda gid: [{"gid": "sec1", "name": "This week"}])
    resp = client.post(
        "/tasks", json={"name": "X", "project": "Chores", "section": "nope"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "This week" in str(resp.json()["detail"])

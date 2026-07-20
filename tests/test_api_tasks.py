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


def test_create_task_renders_structured_content(monkeypatch):
    captured = {}

    def fake_create(fields):
        captured.update(fields)
        return CreatedTask(gid="t9", permalink_url="https://a/t9")

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)

    resp = client.post(
        "/tasks",
        json={
            "name": "Renew passport",
            "context": "expires in October",
            "key_points": ["book appointment"],
            "due_on": "2026-08-01",
        },
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert captured["projects"] == ["p-email"]
    assert captured["due_on"] == "2026-08-01"
    assert captured["name"] == "Renew passport"  # no priority → plain title
    assert captured["html_notes"].startswith("<body>")
    assert "expires in October" in captured["html_notes"]
    assert "<li>book appointment</li>" in captured["html_notes"]
    assert "Created manually" in captured["html_notes"]  # mandatory source footer


def test_create_task_empty_content_gets_source_footer(monkeypatch):
    captured = {}
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: captured.update(fields) or CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    client.post("/tasks", json={"name": "Bare task"}, headers=AUTH)
    assert captured["html_notes"] == "<body><strong>Source:</strong> Created manually</body>"


def test_create_task_priority_prefixes_title(monkeypatch):
    captured = {}
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: captured.update(fields) or CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    client.post("/tasks", json={"name": "Ship it", "priority": "P1"}, headers=AUTH)
    assert captured["name"] == "[P1] Ship it"


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


def _patch_env(monkeypatch, detail=None):
    captured = {"update": None, "added_tags": [], "removed_tags": [], "moved": None}
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: detail or dict(DETAIL))
    monkeypatch.setattr(
        asana, "update_task", lambda gid, fields: captured.__setitem__("update", fields)
    )
    monkeypatch.setattr(asana, "add_tag", lambda gid, tag: captured["added_tags"].append(tag))
    monkeypatch.setattr(asana, "remove_tag", lambda gid, tag: captured["removed_tags"].append(tag))
    monkeypatch.setattr(
        asana,
        "add_task_to_section",
        lambda task_gid, section_gid: captured.__setitem__("moved", section_gid),
    )
    return captured


def test_patch_name_and_completion(monkeypatch):
    captured = _patch_env(monkeypatch)
    resp = client.patch("/tasks/t1", json={"name": "New name", "completed": True}, headers=AUTH)
    assert resp.status_code == 200
    assert captured["update"] == {"name": "New name", "completed": True}


def test_patch_explicit_null_clears_due(monkeypatch):
    captured = _patch_env(monkeypatch)
    client.patch("/tasks/t1", json={"due_on": None}, headers=AUTH)
    assert captured["update"] == {"due_on": None}


def test_patch_rewrites_description_from_content(monkeypatch):
    captured = _patch_env(monkeypatch)
    client.patch("/tasks/t1", json={"key_points": ["new point"]}, headers=AUTH)
    assert "<li>new point</li>" in captured["update"]["html_notes"]


def test_patch_without_content_leaves_description(monkeypatch):
    captured = _patch_env(monkeypatch)
    client.patch("/tasks/t1", json={"name": "New name"}, headers=AUTH)
    assert captured["update"] == {"name": "New name"}  # no html_notes touched


def test_patch_section_move(monkeypatch):
    captured = _patch_env(monkeypatch)
    monkeypatch.setattr(asana, "get_sections", lambda gid: [{"gid": "s-done", "name": "Done"}])
    client.patch("/tasks/t1", json={"section": "done"}, headers=AUTH)
    assert captured["moved"] == "s-done"
    assert captured["update"] is None  # no PUT when only moving section


def test_patch_tags_add_and_remove(monkeypatch):
    from api.routers import tasks as tasks_router

    captured = _patch_env(monkeypatch)
    monkeypatch.setattr(tasks_router.tags_service, "resolve_gids", lambda names: ["tg-new"])
    client.patch(
        "/tasks/t1",
        json={"add_tags": ["urgent"], "remove_tags": ["finance", "ghost"]},
        headers=AUTH,
    )
    assert captured["added_tags"] == ["tg-new"]
    # "finance" resolves from the task's current tags (gid tag1); "ghost" ignored
    assert captured["removed_tags"] == ["tag1"]


def test_patch_unknown_task_404(monkeypatch):
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: None)
    assert client.patch("/tasks/nope", json={"name": "x"}, headers=AUTH).status_code == 404


def test_patch_section_move_task_without_project_400(monkeypatch):
    detail = dict(DETAIL, memberships=[])
    _patch_env(monkeypatch, detail=detail)
    resp = client.patch("/tasks/t1", json={"section": "Done"}, headers=AUTH)
    assert resp.status_code == 400

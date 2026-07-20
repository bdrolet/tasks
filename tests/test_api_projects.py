import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
from api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")


def test_get_projects_includes_sections(monkeypatch):
    monkeypatch.setattr(asana, "list_projects",
                        lambda: [{"gid": "p1", "name": "Home"}])
    monkeypatch.setattr(asana, "get_sections",
                        lambda gid: [{"gid": "s1", "name": "Planning"}])

    resp = client.get("/projects", headers=AUTH)
    assert resp.status_code == 200
    project = resp.json()["projects"][0]
    assert project["name"] == "Home"
    assert project["sections"] == [{"gid": "s1", "name": "Planning"}]


def test_get_tags(monkeypatch):
    monkeypatch.setattr(asana, "list_tags",
                        lambda: [{"gid": "t1", "name": "home"}])
    resp = client.get("/tags", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["tags"] == [{"gid": "t1", "name": "home"}]


def test_create_project(monkeypatch):
    monkeypatch.setattr(
        asana, "create_project",
        lambda name, sections: {
            "gid": "p-new",
            "permalink_url": "https://app.asana.com/x/p-new",
            "sections": {"Planning": "s1"},
        },
    )
    resp = client.post("/projects", headers=AUTH,
                       json={"name": "Reno", "sections": ["Planning"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["project_gid"] == "p-new"
    assert body["permalink_url"] == "https://app.asana.com/x/p-new"
    assert body["sections"] == {"Planning": "s1"}


def test_projects_requires_auth():
    assert client.get("/projects").status_code in (401, 403)

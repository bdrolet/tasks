import httpx
import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
from api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")


def test_add_comment_text(monkeypatch):
    captured = {}

    def fake_create(gid, *, text=None, html_text=None):
        captured.update(gid=gid, text=text, html_text=html_text)
        return {"gid": "s1", "text": text}

    monkeypatch.setattr(asana, "create_story", fake_create)
    resp = client.post("/tasks/t1/comments", json={"text": "note to self"}, headers=AUTH)
    assert resp.status_code == 201
    assert resp.json() == {"comment_gid": "s1", "text": "note to self"}
    assert captured["gid"] == "t1"


def test_add_comment_html_wrapped(monkeypatch):
    captured = {}

    def fake_create(gid, *, text=None, html_text=None):
        captured["html_text"] = html_text
        return {"gid": "s1", "text": "hi"}

    monkeypatch.setattr(asana, "create_story", fake_create)
    client.post("/tasks/t1/comments", json={"html_text": "<b>hi</b>"}, headers=AUTH)
    assert captured["html_text"] == "<body><b>hi</b></body>"


def test_add_comment_requires_exactly_one_body():
    assert client.post("/tasks/t1/comments", json={}, headers=AUTH).status_code == 400
    assert (
        client.post(
            "/tasks/t1/comments", json={"text": "a", "html_text": "b"}, headers=AUTH
        ).status_code
        == 400
    )


def test_edit_comment(monkeypatch):
    captured = {}

    def fake_update(gid, *, text=None, html_text=None):
        captured.update(gid=gid, text=text)

    monkeypatch.setattr(asana, "update_story", fake_update)
    resp = client.put("/comments/s1", json={"text": "edited"}, headers=AUTH)
    assert resp.status_code == 200
    assert captured == {"gid": "s1", "text": "edited"}


def test_edit_foreign_comment_403(monkeypatch):
    def fake_update(gid, *, text=None, html_text=None):
        request = httpx.Request("PUT", "https://app.asana.com/api/1.0/stories/s1")
        response = httpx.Response(
            403, json={"errors": [{"message": "user is not the author"}]}, request=request
        )
        raise httpx.HTTPStatusError("403", request=request, response=response)

    monkeypatch.setattr(asana, "update_story", fake_update)
    resp = client.put("/comments/s1", json={"text": "x"}, headers=AUTH)
    assert resp.status_code == 403


def test_delete_comment(monkeypatch):
    deleted = []
    monkeypatch.setattr(asana, "delete_story", lambda gid: deleted.append(gid))
    resp = client.delete("/comments/s1", headers=AUTH)
    assert resp.status_code == 200
    assert deleted == ["s1"]

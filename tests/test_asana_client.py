import httpx
import pytest

import clients.asana as asana
from tests.test_events import make_email_event


def _resp(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "https://app.asana.com")
    )


@pytest.fixture(autouse=True)
def configure(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_API_KEY", "test-key")
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj-1")
    monkeypatch.setenv("WEBHOOK_URL", "https://inbox-webhook.example")
    monkeypatch.setenv("WEBHOOK_LABEL_TOKEN", "tok")


def _capture(monkeypatch, response):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return response

    monkeypatch.setattr(asana.httpx, "request", fake_request)
    return calls


def test_get_workspace_gid_fetches_and_caches(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", None)
    calls = _capture(monkeypatch, _resp(200, {"data": {"workspace": {"gid": "ws-1"}}}))

    assert asana.get_workspace_gid() == "ws-1"
    assert asana.get_workspace_gid() == "ws-1"

    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/projects/proj-1")
    assert calls[0]["params"] == {"opt_fields": "workspace"}


def test_find_tag_returns_matching_gid(monkeypatch):
    calls = _capture(
        monkeypatch,
        _resp(
            200,
            {
                "data": [
                    {"gid": "t1", "name": "Other"},
                    {"gid": "t2", "name": "URGENT"},
                ]
            },
        ),
    )
    assert asana.find_tag("urgent", "ws-1") == "t2"
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/workspaces/ws-1/typeahead")
    assert calls[0]["params"] == {"resource_type": "tag", "query": "urgent"}


def test_find_tag_returns_none_when_no_match(monkeypatch):
    _capture(monkeypatch, _resp(200, {"data": [{"gid": "t1", "name": "Other"}]}))
    assert asana.find_tag("urgent", "ws-1") is None


def test_create_tag_posts_and_returns_gid(monkeypatch):
    calls = _capture(monkeypatch, _resp(201, {"data": {"gid": "t9"}}))
    assert asana.create_tag("Urgent", "ws-1") == "t9"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/tags")
    assert calls[0]["json"] == {"data": {"name": "Urgent", "workspace": "ws-1"}}


def test_create_task_builds_payload(monkeypatch):
    calls = _capture(
        monkeypatch, _resp(201, {"data": {"gid": "42", "permalink_url": "https://a/42"}})
    )
    task = asana.create_task(make_email_event(), tag_gids=["tg1"], due_date="2026-07-20")
    assert task is not None and task.gid == "42"
    payload = calls[0]["json"]["data"]
    assert payload["name"] == "[P1] Quarterly report"
    assert payload["external"] == {"gid": "msg-123", "data": "inbox"}
    assert payload["due_on"] == "2026-07-20"
    assert payload["tags"] == ["tg1"]
    assert payload["projects"] == ["proj-1"]
    assert "Confirmed review" in payload["html_notes"]
    assert "Respond instead" in payload["html_notes"]
    assert "<li><strong>To:</strong> ben@drolet.cloud</li>" in payload["html_notes"]
    assert "<li><strong>Cc:</strong> team@example.com</li>" in payload["html_notes"]


def test_create_task_key_points_render(monkeypatch):
    calls = _capture(
        monkeypatch, _resp(201, {"data": {"gid": "42", "permalink_url": "https://a/42"}})
    )
    asana.create_task(
        make_email_event(),
        key_points=["Point one"],
        relevant_links=[["https://x", "Doc"]],
    )
    notes = calls[0]["json"]["data"]["html_notes"]
    assert "<li>Point one</li>" in notes
    assert '<a href="https://x">Doc</a>' in notes


def test_create_task_preview_fallback_without_key_points(monkeypatch):
    calls = _capture(
        monkeypatch, _resp(201, {"data": {"gid": "42", "permalink_url": "https://a/42"}})
    )
    asana.create_task(make_email_event(body="x" * 900))
    notes = calls[0]["json"]["data"]["html_notes"]
    assert "Preview:" in notes
    assert "x" * 500 + "..." in notes


def test_create_task_duplicate_returns_none(monkeypatch):
    _capture(
        monkeypatch,
        _resp(400, {"errors": [{"message": "external: Already assigned to another object"}]}),
    )
    assert asana.create_task(make_email_event()) is None


def test_create_task_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_API_KEY", "")
    assert asana.create_task(make_email_event()) is None


def test_add_task_to_section(monkeypatch):
    calls = _capture(monkeypatch, _resp(200, {"data": {}}))
    asana.add_task_to_section("42", "sec-1")
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/sections/sec-1/addTask")
    assert calls[0]["json"] == {"data": {"task": "42"}}


def test_complete_task_uses_put(monkeypatch):
    calls = _capture(monkeypatch, _resp(200, {"data": {}}))
    asana.complete_task("42")
    assert calls[0]["method"] == "PUT"
    assert calls[0]["json"] == {"data": {"completed": True}}


def test_find_task_by_external_returns_none_on_404(monkeypatch):
    _capture(monkeypatch, _resp(404, {"errors": [{"message": "Not found"}]}))
    assert asana.find_task_by_external("msg-999") is None


def test_get_incomplete_tasks_past_due_filters_by_due_date(monkeypatch):
    _capture(
        monkeypatch,
        _resp(
            200,
            {
                "data": [
                    {"gid": "1", "due_on": "2020-01-01", "memberships": []},
                    {"gid": "2", "due_on": "2999-01-01", "memberships": []},
                    {"gid": "3", "due_on": None, "memberships": []},
                ]
            },
        ),
    )
    overdue = asana.get_incomplete_tasks_past_due()
    assert [t["gid"] for t in overdue] == ["1"]


def test_current_section_picks_this_project(monkeypatch):
    task = {
        "memberships": [
            {"project": {"gid": "other"}, "section": {"gid": "sX", "name": "Nope"}},
            {"project": {"gid": "proj-1"}, "section": {"gid": "s1", "name": "Review"}},
        ]
    }
    assert asana.current_section(task) == {"gid": "s1", "name": "Review"}

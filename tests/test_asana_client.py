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
    task = asana.create_task(
        make_email_event(), tag_gids=["tg1"], due_date="2026-07-20", html_notes="<body>hi</body>"
    )
    assert task is not None and task.gid == "42"
    payload = calls[0]["json"]["data"]
    assert payload["name"] == "[P1] Quarterly report"
    assert payload["external"] == {"gid": "msg-123", "data": "inbox"}
    assert payload["due_on"] == "2026-07-20"
    assert payload["tags"] == ["tg1"]
    assert payload["projects"] == ["proj-1"]
    assert payload["html_notes"] == "<body>hi</body>"  # passed through, not built here


def test_create_task_uses_enriched_title(monkeypatch):
    calls = _capture(
        monkeypatch, _resp(201, {"data": {"gid": "42", "permalink_url": "https://a/42"}})
    )
    asana.create_task(make_email_event(), title="[P1] Review the quarterly report")
    assert calls[0]["json"]["data"]["name"] == "[P1] Review the quarterly report"


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


def _capture_seq(monkeypatch, responses):
    """Like _capture but returns responses in order, one per call."""
    calls = []
    it = iter(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return next(it)

    monkeypatch.setattr(asana.httpx, "request", fake_request)
    return calls


def test_list_projects_paginates(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(
        monkeypatch,
        [
            _resp(200, {"data": [{"gid": "p1", "name": "Inbox"}], "next_page": {"offset": "abc"}}),
            _resp(200, {"data": [{"gid": "p2", "name": "Chores"}], "next_page": None}),
        ],
    )
    projects = asana.list_projects()
    assert [p["gid"] for p in projects] == ["p1", "p2"]
    assert calls[0]["url"].endswith("/projects")
    assert calls[0]["params"]["workspace"] == "ws-1"
    assert calls[0]["params"]["archived"] == "false"
    assert calls[0]["params"]["limit"] == 100
    assert "offset" not in calls[0]["params"]
    assert calls[1]["params"]["offset"] == "abc"


def test_list_project_tasks_single_page(monkeypatch):
    calls = _capture_seq(
        monkeypatch,
        [_resp(200, {"data": [{"gid": "t1", "name": "A", "notes": "", "completed": False}]})],
    )
    tasks = asana.list_project_tasks("p1")
    assert tasks[0]["gid"] == "t1"
    assert calls[0]["params"]["project"] == "p1"
    assert calls[0]["params"]["opt_fields"] == asana.SEARCH_OPT_FIELDS


def test_list_project_tasks_only_open_adds_completed_since(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": []})])
    asana.list_project_tasks("p1", only_open=True)
    assert calls[0]["params"]["completed_since"] == "now"


def test_list_project_tasks_default_omits_completed_since(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": []})])
    asana.list_project_tasks("p1")
    assert "completed_since" not in calls[0]["params"]


def test_list_my_tasks(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": [{"gid": "t9"}]})])
    assert asana.list_my_tasks()[0]["gid"] == "t9"
    assert calls[0]["params"]["assignee"] == "me"
    assert calls[0]["params"]["workspace"] == "ws-1"


def test_list_my_tasks_only_open_adds_completed_since(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": []})])
    asana.list_my_tasks(only_open=True)
    assert calls[0]["params"]["completed_since"] == "now"


def test_list_my_tasks_default_omits_completed_since(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": []})])
    asana.list_my_tasks()
    assert "completed_since" not in calls[0]["params"]


def test_get_task_detail_returns_none_on_404(monkeypatch):
    _capture_seq(monkeypatch, [_resp(404, {"errors": [{"message": "Not Found"}]})])
    assert asana.get_task_detail("nope") is None


def test_get_task_detail_fetches_rich_fields(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": {"gid": "t1", "name": "A"}})])
    task = asana.get_task_detail("t1")
    assert task == {"gid": "t1", "name": "A"}
    assert calls[0]["url"].endswith("/tasks/t1")
    assert calls[0]["params"]["opt_fields"] == asana.DETAIL_OPT_FIELDS


def test_get_stories(monkeypatch):
    calls = _capture_seq(
        monkeypatch,
        [_resp(200, {"data": [{"gid": "s1", "type": "comment", "text": "hi"}]})],
    )
    stories = asana.get_stories("t1")
    assert stories[0]["gid"] == "s1"
    assert calls[0]["url"].endswith("/tasks/t1/stories")


def test_create_task_from_fields(monkeypatch):
    calls = _capture_seq(
        monkeypatch, [_resp(201, {"data": {"gid": "77", "permalink_url": "https://a/77"}})]
    )
    created = asana.create_task_from_fields({"name": "Buy milk", "projects": ["p1"]})
    assert created.gid == "77"
    assert created.permalink_url == "https://a/77"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/tasks")
    assert calls[0]["json"] == {"data": {"name": "Buy milk", "projects": ["p1"]}}


def test_update_task_puts_fields(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": {"gid": "77"}})])
    asana.update_task("77", {"completed": True, "due_on": None})
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"].endswith("/tasks/77")
    assert calls[0]["json"] == {"data": {"completed": True, "due_on": None}}


def test_remove_tag(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": {}})])
    asana.remove_tag("77", "tag-1")
    assert calls[0]["url"].endswith("/tasks/77/removeTag")
    assert calls[0]["json"] == {"data": {"tag": "tag-1"}}


def test_create_story_text(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(201, {"data": {"gid": "s1", "text": "hi"}})])
    story = asana.create_story("77", text="hi")
    assert story["gid"] == "s1"
    assert calls[0]["url"].endswith("/tasks/77/stories")
    assert calls[0]["json"] == {"data": {"text": "hi"}}


def test_create_story_html(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(201, {"data": {"gid": "s1"}})])
    asana.create_story("77", html_text="<body>hi</body>")
    assert calls[0]["json"] == {"data": {"html_text": "<body>hi</body>"}}


def test_update_story(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": {"gid": "s1"}})])
    asana.update_story("s1", text="edited")
    assert calls[0]["method"] == "PUT"
    assert calls[0]["url"].endswith("/stories/s1")
    assert calls[0]["json"] == {"data": {"text": "edited"}}


def test_delete_story(monkeypatch):
    calls = _capture_seq(monkeypatch, [_resp(200, {"data": {}})])
    asana.delete_story("s1")
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/stories/s1")


def test_list_tags_returns_workspace_tags(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture(
        monkeypatch,
        _resp(
            200,
            {
                "data": [{"gid": "t1", "name": "home"}, {"gid": "t2", "name": "urgent"}],
                "next_page": None,
            },
        ),
    )
    assert asana.list_tags() == [
        {"gid": "t1", "name": "home"},
        {"gid": "t2", "name": "urgent"},
    ]
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/workspaces/ws-1/tags")
    assert calls[0]["params"]["opt_fields"] == "name"


def test_create_project_creates_project_then_sections(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(
        monkeypatch,
        [
            _resp(
                201,
                {"data": {"gid": "proj-new", "permalink_url": "https://app.asana.com/x/proj-new"}},
            ),
            _resp(201, {"data": {"gid": "sec-a", "name": "Planning"}}),
            _resp(201, {"data": {"gid": "sec-b", "name": "Build"}}),
        ],
    )

    result = asana.create_project("Kitchen Remodel", ["Planning", "Build"])

    assert result["gid"] == "proj-new"
    assert result["permalink_url"] == "https://app.asana.com/x/proj-new"
    assert result["sections"] == {"Planning": "sec-a", "Build": "sec-b"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/projects")
    assert calls[0]["json"]["data"] == {"name": "Kitchen Remodel", "workspace": "ws-1"}
    assert calls[1]["url"].endswith("/projects/proj-new/sections")
    assert calls[1]["json"]["data"] == {"name": "Planning"}
    assert calls[2]["json"]["data"] == {"name": "Build"}


def test_create_project_no_sections(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(
        monkeypatch,
        [_resp(201, {"data": {"gid": "proj-x", "permalink_url": None}})],
    )
    result = asana.create_project("Solo")
    assert result["gid"] == "proj-x"
    assert result["sections"] == {}
    assert len(calls) == 1

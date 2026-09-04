import json

import httpx
import pytest

import clients.schedule_api as sapi


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SCHEDULE_API_URL", "https://sched.example")
    monkeypatch.setenv("SCHEDULE_API_TOKEN", "tok")


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sapi, "_client", lambda: httpx.Client(transport=transport))


def test_create_event_posts_all_day_transparent(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = request.read()
        return httpx.Response(
            201, json={"event_id": "e1", "calendar_id": "primary", "html_link": "h"}
        )

    _mock(monkeypatch, handler)
    out = sapi.create_event(
        calendar="primary", day="2026-09-10", title="1 task due", sections=[{"title": "T"}]
    )
    assert out["event_id"] == "e1"
    assert seen["url"] == "https://sched.example/events" and seen["auth"] == "Bearer tok"
    payload = json.loads(seen["json"])
    assert payload == {
        "calendar": "primary",
        "date": "2026-09-10",
        "title": "1 task due",
        "sections": [{"title": "T"}],
        "transparency": "transparent",
    }


def test_patch_event_404_raises_not_found(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"detail": "gone"}))
    with pytest.raises(sapi.NotFound):
        sapi.patch_event("e1", calendar="c", title="t", sections=[])


def test_patch_event_sends_calendar_query(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(
            200, json={"status": "updated", "event_id": "e1", "calendar_id": "c", "fields": []}
        )

    _mock(monkeypatch, handler)
    sapi.patch_event("e1", calendar="c", title="t", sections=[])
    assert seen["method"] == "PATCH" and seen["url"] == "https://sched.example/events/e1?calendar=c"


def test_delete_event_treats_404_as_success(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"detail": "gone"}))
    sapi.delete_event("e1", calendar="c")  # no raise


def test_other_errors_raise(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        sapi.delete_event("e1", calendar="c")


def test_search_digest_events_filters_to_day(monkeypatch):
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "event_id": "a",
                        "start": "2026-09-10",
                        "all_day": True,
                        "title": "2 tasks due",
                    },
                    {
                        "event_id": "b",
                        "start": "2026-09-11",
                        "all_day": True,
                        "title": "1 task due",
                    },
                ],
                "window": {"time_min": "", "time_max": ""},
                "calendars_searched": ["c"],
            },
        )

    _mock(monkeypatch, handler)
    out = sapi.search_digest_events(calendar="c", day="2026-09-10")
    assert [r["event_id"] for r in out] == ["a"]
    assert seen["json"]["calendar"] == "c" and seen["json"]["all_day"] is True
    # Single term: Google's `q` is term/prefix matching, so "tasks due" would
    # miss a "1 task due" title. The caller filters titles by regex.
    assert seen["json"]["query"] == "due"
    assert seen["json"]["time_min"].startswith("2026-09-09") and seen["json"][
        "time_max"
    ].startswith("2026-09-11")

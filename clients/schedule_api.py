"""Client for the schedule-api Cloud Run service — the calendar gateway.

This repo never talks to Google Calendar directly (schedule owns every
calendar write, its dedup and routing invariants included). The due-day
digest (handlers/due_digest.py) is the first consumer: all-day events with
a `sections` body — see ~/src/schedule/docs/event-content-standard.md."""

import os
from datetime import date, timedelta

import httpx

TIMEOUT = 30


class NotFound(Exception):
    """The event is gone on the calendar (deleted by hand)."""


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT)


def _url(path: str) -> str:
    return f"{os.environ.get('SCHEDULE_API_URL', '')}{path}"


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ.get('SCHEDULE_API_TOKEN', '')}"}


def create_event(*, calendar: str, day: str, title: str, sections: list[dict]) -> dict:
    payload = {
        "calendar": calendar,
        "date": day,
        "title": title,
        "sections": sections,
        "transparency": "transparent",
    }
    with _client() as client:
        resp = client.post(_url("/events"), json=payload, headers=_headers())
    resp.raise_for_status()
    return resp.json()


def patch_event(event_id: str, *, calendar: str, title: str, sections: list[dict]) -> dict:
    with _client() as client:
        resp = client.patch(
            _url(f"/events/{event_id}"),
            params={"calendar": calendar},
            json={"title": title, "sections": sections},
            headers=_headers(),
        )
    if resp.status_code == 404:
        raise NotFound(event_id)
    resp.raise_for_status()
    return resp.json()


def delete_event(event_id: str, *, calendar: str) -> None:
    with _client() as client:
        resp = client.delete(
            _url(f"/events/{event_id}"), params={"calendar": calendar}, headers=_headers()
        )
    if resp.status_code == 404:
        return
    resp.raise_for_status()


def search_digest_events(*, calendar: str, day: str) -> list[dict]:
    r"""All-day events on `calendar` that start on `day`. The window is padded
    a day each side because all-day events are matched by overlap in UTC. The
    query is the single term `due` — Google's `q` is term/prefix matching, and
    "tasks due" would miss a "1 task due" title; the caller filters titles
    against `^\d+ tasks? due$`."""
    d = date.fromisoformat(day)
    payload = {
        "query": "due",
        "calendar": calendar,
        "time_min": f"{(d - timedelta(days=1)).isoformat()}T00:00:00Z",
        "time_max": f"{(d + timedelta(days=1)).isoformat()}T23:59:59Z",
        "all_day": True,
        "limit": 50,
    }
    with _client() as client:
        resp = client.post(_url("/search"), json=payload, headers=_headers())
    resp.raise_for_status()
    return [r for r in resp.json().get("results", []) if (r.get("start") or "").startswith(day)]

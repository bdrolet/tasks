import hashlib
import hmac
import json

import pytest

from handlers import asana_webhook

SECRET = "whsec"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("ASANA_WEBHOOK_SECRET", SECRET)


def _signed(events):
    body = json.dumps({"events": events}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def _capture(monkeypatch):
    refreshed, completed = [], []
    monkeypatch.setattr(asana_webhook.task_index, "refresh", refreshed.append)
    monkeypatch.setattr(asana_webhook.task_complete, "handle", completed.append)
    return refreshed, completed


def test_added_task_event_refreshes(monkeypatch):
    refreshed, completed = _capture(monkeypatch)
    body, sig = _signed(
        [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig) == ("", 200)
    assert refreshed == ["t1"]
    assert completed == []


def test_changed_name_event_refreshes(monkeypatch):
    refreshed, _ = _capture(monkeypatch)
    body, sig = _signed(
        [
            {
                "action": "changed",
                "resource": {"gid": "t2", "resource_type": "task"},
                "change": {"field": "name"},
            }
        ]
    )
    asana_webhook.receive(body, sig)
    assert refreshed == ["t2"]


def test_completed_event_still_completes_not_refreshes(monkeypatch):
    refreshed, completed = _capture(monkeypatch)
    body, sig = _signed(
        [
            {
                "action": "changed",
                "resource": {"gid": "t3", "resource_type": "task"},
                "change": {"field": "completed"},
            }
        ]
    )
    asana_webhook.receive(body, sig)
    assert completed == ["t3"]
    assert refreshed == []


def test_duplicate_gids_refresh_once(monkeypatch):
    refreshed, _ = _capture(monkeypatch)
    events = [
        {
            "action": "changed",
            "resource": {"gid": "t4", "resource_type": "task"},
            "change": {"field": "name"},
        },
        {
            "action": "changed",
            "resource": {"gid": "t4", "resource_type": "task"},
            "change": {"field": "notes"},
        },
    ]
    body, sig = _signed(events)
    asana_webhook.receive(body, sig)
    assert refreshed == ["t4"]


def test_non_task_and_irrelevant_events_ignored(monkeypatch):
    refreshed, completed = _capture(monkeypatch)
    body, sig = _signed(
        [
            {"action": "added", "resource": {"gid": "s1", "resource_type": "story"}},
            {
                "action": "changed",
                "resource": {"gid": "t5", "resource_type": "task"},
                "change": {"field": "assignee"},
            },
        ]
    )
    asana_webhook.receive(body, sig)
    assert refreshed == []
    assert completed == []


def test_bad_signature_rejected(monkeypatch):
    refreshed, _ = _capture(monkeypatch)
    body, _ = _signed([])
    assert asana_webhook.receive(body, "bogus") == ("", 401)

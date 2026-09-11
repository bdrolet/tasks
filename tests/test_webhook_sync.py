import json

import clients.asana as asana
from handlers import webhook_sync
from services import managed_projects
from tests.test_repo import FakeConn  # noqa: F401 — available for write-path assertions
from tests.test_repo_due_digest import RowsConn

BASE = "https://cf.example/tasks-webhook"


def _managed(monkeypatch, *gids):
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({g: {"done": None} for g in gids}))


def test_registers_a_missing_project(monkeypatch):
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [])
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))
    created = []
    monkeypatch.setattr(
        asana,
        "create_webhook",
        lambda resource, target: created.append((resource, target)) or {"gid": "w1"},
    )
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: None)

    result = webhook_sync.run(BASE)
    assert created == [("p1", f"{BASE}?project=p1")]
    assert result == {"managed": 1, "registered": 1, "deleted": 0, "active": 1}


def test_leaves_the_legacy_webhook_alone(monkeypatch):
    """A webhook whose target carries no ?project= is the pre-rollout one (D7)."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [{"gid": "legacy", "target": BASE}])
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))
    deleted = []
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: deleted.append(gid))
    monkeypatch.setattr(asana, "create_webhook", lambda resource, target: {"gid": "w1"})

    webhook_sync.run(BASE)
    assert deleted == []


def test_deregisters_a_project_that_left_the_map(monkeypatch):
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [
            {"gid": "w1", "target": f"{BASE}?project=p1"},
            {"gid": "w9", "target": f"{BASE}?project=p9"},
        ],
    )
    monkeypatch.setattr(
        webhook_sync,
        "get_conn",
        lambda: RowsConn(rows=[{"project_gid": "p1"}, {"project_gid": "p9"}]),
    )
    deleted = []
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: deleted.append(gid))
    monkeypatch.setattr(asana, "create_webhook", lambda resource, target: {"gid": "new"})

    result = webhook_sync.run(BASE)
    assert deleted == ["w9"]
    assert result["deleted"] == 1
    assert result["active"] == 1


def test_steady_state_changes_nothing(monkeypatch):
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana, "list_webhooks", lambda: [{"gid": "w1", "target": f"{BASE}?project=p1"}]
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))
    monkeypatch.setattr(
        asana, "create_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no create"))
    )
    monkeypatch.setattr(
        asana, "delete_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no delete"))
    )

    assert webhook_sync.run(BASE) == {
        "managed": 1,
        "registered": 0,
        "deleted": 0,
        "active": 1,
    }


def test_a_failed_registration_does_not_stop_the_others(monkeypatch):
    _managed(monkeypatch, "p1", "p2")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [])
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))

    def flaky(resource, target):
        if resource == "p1":
            raise RuntimeError("asana 500")
        return {"gid": "w2"}

    monkeypatch.setattr(asana, "create_webhook", flaky)
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: None)

    result = webhook_sync.run(BASE)
    assert result["registered"] == 1
    assert result["active"] == 1

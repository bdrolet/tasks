import json

import clients.asana as asana
from handlers import webhook_sync
from services import managed_projects
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


def test_replaces_a_live_webhook_with_no_secret_row(monkeypatch):
    """A managed project's webhook with no asana_webhooks row is unvalidatable —
    it must be deleted and re-registered in that order (the reason deletes run
    before registrations at all)."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana, "list_webhooks", lambda: [{"gid": "w1", "target": f"{BASE}?project=p1"}]
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))  # no secret row

    calls = []
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: calls.append(("delete", gid)))
    monkeypatch.setattr(
        asana,
        "create_webhook",
        lambda resource, target: calls.append(("create", resource, target)) or {"gid": "new"},
    )

    result = webhook_sync.run(BASE)
    assert result == {"managed": 1, "registered": 1, "deleted": 1, "active": 1}
    assert calls == [("delete", "w1"), ("create", "p1", f"{BASE}?project=p1")]


def test_a_failed_delete_still_proceeds_to_register(monkeypatch):
    """plan.to_register is computed up front, before any delete runs, so a
    delete failure does not block the re-registration of the same project —
    it transiently leaves two live Asana webhooks for that project. That is
    acceptable and deliberate: the dead one self-heals via Asana's 24-hour
    eviction, and the next handshake's ON CONFLICT overwrites the secret row.
    A second, unrelated managed project in the same run must be unaffected."""
    _managed(monkeypatch, "p1", "p2")
    monkeypatch.setattr(
        asana, "list_webhooks", lambda: [{"gid": "w1", "target": f"{BASE}?project=p1"}]
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))  # no secret rows

    def flaky_delete(gid):
        raise RuntimeError("asana 500")

    created = []
    monkeypatch.setattr(asana, "delete_webhook", flaky_delete)
    monkeypatch.setattr(
        asana,
        "create_webhook",
        lambda resource, target: created.append((resource, target)) or {"gid": "new"},
    )

    result = webhook_sync.run(BASE)
    # p1's failed delete does not count, but registration still proceeds for
    # both p1 (the replace) and p2 (a plain missing registration).
    assert result["deleted"] == 0
    assert result["registered"] == 2
    assert created == [("p1", f"{BASE}?project=p1"), ("p2", f"{BASE}?project=p2")]
    # p1 was already counted live (its old, undeleted webhook) and stays live
    # after re-registration; p2 becomes live. Both managed projects report
    # active despite the stray duplicate webhook on Asana's side.
    assert result["active"] == 2


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

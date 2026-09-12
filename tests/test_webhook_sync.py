import json

import pytest

import clients.asana as asana
from handlers import webhook_sync
from services import managed_projects, webhook_registry
from tests.test_repo_due_digest import RowsConn

BASE = "https://cf.example/tasks-webhook"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    """The reconciler signs every target it registers (see Finding 1)."""
    monkeypatch.setenv(webhook_registry._SIGNING_KEY_ENV, "escalate-bearer")


def _target(gid):
    return webhook_registry.target_for(BASE, gid)


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
    assert created == [("p1", _target("p1"))]
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
    assert calls == [("delete", "w1"), ("create", "p1", _target("p1"))]


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
    assert created == [("p1", _target("p1")), ("p2", _target("p2"))]
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


def test_an_empty_managed_map_deletes_nothing_and_reports_the_refusal(monkeypatch, caplog):
    """An unset or blank ASANA_MANAGED_PROJECTS degrades to {}, which would
    otherwise put every project webhook into to_delete and end recurrence
    everywhere — reachable by a missing GitHub repo variable, not only by
    intent."""
    monkeypatch.delenv(managed_projects.ENV_VAR, raising=False)
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [
            {"gid": "w1", "target": f"{BASE}?project=p1"},
            {"gid": "w2", "target": f"{BASE}?project=p2"},
        ],
    )
    monkeypatch.setattr(
        webhook_sync,
        "get_conn",
        lambda: RowsConn(rows=[{"project_gid": "p1"}, {"project_gid": "p2"}]),
    )
    monkeypatch.setattr(
        asana, "delete_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no delete"))
    )
    monkeypatch.setattr(
        asana, "create_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no create"))
    )

    result = webhook_sync.run(BASE)
    assert result == {
        "managed": 0,
        "registered": 0,
        "deleted": 0,
        "active": 0,
        "refused_deletes": 2,
    }
    assert any(
        record.levelname == "ERROR" and "refusing to delete" in record.getMessage()
        for record in caplog.records
    )


def test_a_blank_managed_map_also_refuses(monkeypatch):
    """TF_VAR_asana_managed_projects renders an undefined repo variable as ""."""
    monkeypatch.setenv(managed_projects.ENV_VAR, "")
    monkeypatch.setattr(
        asana, "list_webhooks", lambda: [{"gid": "w1", "target": f"{BASE}?project=p1"}]
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))
    monkeypatch.setattr(
        asana, "delete_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no delete"))
    )

    assert webhook_sync.run(BASE)["refused_deletes"] == 1


def test_an_empty_managed_map_with_nothing_registered_is_a_plain_no_op(monkeypatch):
    """Nothing to refuse — the ordinary result shape, no refused_deletes key."""
    monkeypatch.setenv(managed_projects.ENV_VAR, "{}")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [{"gid": "legacy", "target": BASE}])
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))

    assert webhook_sync.run(BASE) == {
        "managed": 0,
        "registered": 0,
        "deleted": 0,
        "active": 0,
    }


def test_a_populated_map_still_deletes_an_unmanaged_projects_webhook(monkeypatch):
    """The safety valve is scoped to the empty map; deliberate deregistration
    of one project among several is unaffected."""
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
    assert "refused_deletes" not in result


def test_an_inactive_webhook_is_replaced_and_does_not_count_as_live(monkeypatch):
    """asana.webhooks.active must measure delivery health, not "the target
    string parses" — an inactive webhook is delivering nothing."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [
            {
                "gid": "w1",
                "target": f"{BASE}?project=p1",
                "active": False,
                "resource": {"gid": "p1"},
            }
        ],
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))

    calls = []
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: calls.append(("delete", gid)))
    monkeypatch.setattr(
        asana,
        "create_webhook",
        lambda resource, target: calls.append(("create", resource, target)) or {"gid": "w2"},
    )

    gauges = []
    monkeypatch.setattr(webhook_sync.otel.webhooks_active, "set", gauges.append)

    result = webhook_sync.run(BASE)
    assert calls == [("delete", "w1"), ("create", "p1", _target("p1"))]
    assert result == {"managed": 1, "registered": 1, "deleted": 1, "active": 1}
    assert gauges == [1]  # healthy again only because it was replaced


def test_an_inactive_webhook_that_cannot_be_replaced_is_not_counted_live(monkeypatch):
    """The gauge has to fall when the repair itself fails — that is the alert."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [{"gid": "w1", "target": f"{BASE}?project=p1", "active": False}],
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: None)

    def flaky(resource, target):
        raise RuntimeError("asana 500")

    monkeypatch.setattr(asana, "create_webhook", flaky)

    result = webhook_sync.run(BASE)
    assert result == {"managed": 1, "registered": 0, "deleted": 1, "active": 0}


def test_an_active_webhook_is_left_alone(monkeypatch):
    """active: true, and the missing-field case, are both healthy."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [
            {"gid": "w1", "target": f"{BASE}?project=p1", "active": True, "resource": {"gid": "p1"}}
        ],
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))
    monkeypatch.setattr(
        asana, "delete_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no delete"))
    )
    monkeypatch.setattr(
        asana, "create_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no create"))
    )

    assert webhook_sync.run(BASE)["active"] == 1


def test_a_resource_target_mismatch_is_skipped_with_a_log(monkeypatch, caplog):
    """A webhook whose target names one project while Asana has it registered
    on another resource is an inconsistency we must not act on."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [
            {"gid": "w1", "target": f"{BASE}?project=p1", "resource": {"gid": "p-somewhere-else"}}
        ],
    )
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}]))
    monkeypatch.setattr(
        asana, "delete_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no delete"))
    )
    created = []
    monkeypatch.setattr(
        asana,
        "create_webhook",
        lambda resource, target: created.append((resource, target)) or {"gid": "w2"},
    )

    result = webhook_sync.run(BASE)
    # The mismatched webhook is invisible to the diff, so p1 simply looks
    # unregistered and gets a fresh, correct one. The stray is never deleted.
    assert created == [("p1", _target("p1"))]
    assert result == {"managed": 1, "registered": 1, "deleted": 0, "active": 1}
    assert any(
        record.levelname == "ERROR" and "registered on resource" in record.getMessage()
        for record in caplog.records
    )

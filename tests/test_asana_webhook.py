import hashlib
import hmac
import json

import pytest

from handlers import asana_webhook
from services import managed_projects, webhook_registry
from tests.test_repo import FakeConn

SECRET = "whsec"
ESCALATE_TOKEN = "escalate-bearer"
MANAGED_PROJECT = "p-family"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("ASANA_WEBHOOK_SECRET", SECRET)
    # The key behind the target URL's `t` token (services/webhook_registry).
    monkeypatch.setenv("ASANA_ESCALATE_TOKEN", ESCALATE_TOKEN)
    monkeypatch.setenv(
        managed_projects.ENV_VAR, json.dumps({MANAGED_PROJECT: {"done": "sec-done"}})
    )


def _signed(events):
    body = json.dumps({"events": events}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def _capture(monkeypatch):
    refreshed, completed, removed = [], [], []
    monkeypatch.setattr(asana_webhook.task_index, "refresh", refreshed.append)
    monkeypatch.setattr(asana_webhook.task_index, "remove", removed.append)
    monkeypatch.setattr(asana_webhook.task_complete, "handle", completed.append)
    return refreshed, completed, removed


def test_added_task_event_refreshes(monkeypatch):
    refreshed, completed, _ = _capture(monkeypatch)
    body, sig = _signed([{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)
    assert refreshed == ["t1"]
    assert completed == []


def test_changed_name_event_refreshes(monkeypatch):
    refreshed, _, _ = _capture(monkeypatch)
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
    refreshed, completed, _ = _capture(monkeypatch)
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
    refreshed, _, _ = _capture(monkeypatch)
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
    refreshed, completed, _ = _capture(monkeypatch)
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
    refreshed, _, _ = _capture(monkeypatch)
    body, _ = _signed([])
    assert asana_webhook.receive(body, "bogus") == ("", 401)


def test_deleted_task_event_removes(monkeypatch):
    refreshed, completed, removed = _capture(monkeypatch)
    body, sig = _signed([{"action": "deleted", "resource": {"gid": "t6", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)
    assert removed == ["t6"]
    assert refreshed == []
    assert completed == []


def test_removed_task_event_removes(monkeypatch):
    refreshed, _, removed = _capture(monkeypatch)
    body, sig = _signed([{"action": "removed", "resource": {"gid": "t7", "resource_type": "task"}}])
    asana_webhook.receive(body, sig)
    assert removed == ["t7"]
    assert refreshed == []


def test_gid_changed_and_deleted_in_same_delivery_only_removed(monkeypatch):
    refreshed, _, removed = _capture(monkeypatch)
    events = [
        {
            "action": "changed",
            "resource": {"gid": "t8", "resource_type": "task"},
            "change": {"field": "name"},
        },
        {"action": "deleted", "resource": {"gid": "t8", "resource_type": "task"}},
    ]
    body, sig = _signed(events)
    asana_webhook.receive(body, sig)
    assert removed == ["t8"]
    assert refreshed == []


def test_refresh_burst_capped_at_20(monkeypatch):
    refreshed, _, _ = _capture(monkeypatch)
    events = [
        {
            "action": "changed",
            "resource": {"gid": f"t{i}", "resource_type": "task"},
            "change": {"field": "name"},
        }
        for i in range(21)
    ]
    body, sig = _signed(events)
    asana_webhook.receive(body, sig)
    assert len(refreshed) == 20


def test_relevant_events_mark_digest_dirty(monkeypatch):
    _capture(monkeypatch)
    marks = []
    monkeypatch.setattr(asana_webhook, "_mark_digest_dirty", lambda: marks.append(1))
    body, sig = _signed(
        [
            {
                "action": "changed",
                "resource": {"gid": "t1", "resource_type": "task"},
                "change": {"field": "due_on"},
            }
        ]
    )
    asana_webhook.receive(body, sig)
    assert marks == [1]


def test_irrelevant_events_do_not_mark_dirty(monkeypatch):
    _capture(monkeypatch)
    marks = []
    monkeypatch.setattr(asana_webhook, "_mark_digest_dirty", lambda: marks.append(1))
    body, sig = _signed(
        [{"action": "changed", "resource": {"gid": "s1", "resource_type": "story"}}]
    )
    asana_webhook.receive(body, sig)
    assert marks == []


def test_dirty_flag_db_failure_does_not_fail_delivery(monkeypatch):
    _capture(monkeypatch)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(asana_webhook, "get_conn", boom)
    body, sig = _signed([{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)


PROJECT_SECRET = "per-project"


@pytest.fixture(autouse=True)
def _clear_secret_cache():
    asana_webhook._secret_cache.clear()
    yield
    asana_webhook._secret_cache.clear()


def _signed_with(secret, events):
    body = json.dumps({"events": events}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def test_per_project_secret_validates(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(
        asana_webhook, "get_conn", lambda: FakeConn(row={"secret": PROJECT_SECRET})
    )
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-family") == ("", 200)


def test_another_projects_secret_is_rejected(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(
        asana_webhook, "get_conn", lambda: FakeConn(row={"secret": PROJECT_SECRET})
    )
    body, sig = _signed_with(
        "wrong-secret", [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-family") == ("", 401)


def test_no_project_parameter_falls_back_to_the_env_secret(monkeypatch):
    """D7: the legacy webhook keeps working through the rollout."""
    _capture(monkeypatch)
    body, sig = _signed([{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)


def test_a_cached_secret_avoids_the_database(monkeypatch):
    _capture(monkeypatch)
    # Isolate the secret-cache read path from the pre-existing digest-dirty
    # write path (also `get_conn`, exercised separately above) so this test
    # counts only secret lookups.
    monkeypatch.setattr(asana_webhook, "_mark_digest_dirty", lambda: None)
    reads = []

    def counting_conn():
        reads.append(1)
        return FakeConn(row={"secret": PROJECT_SECRET})

    monkeypatch.setattr(asana_webhook, "get_conn", counting_conn)
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    asana_webhook.receive(body, sig, "p-family")
    asana_webhook.receive(body, sig, "p-family")
    assert len(reads) == 1


def test_an_expired_cache_entry_is_re_read(monkeypatch):
    _capture(monkeypatch)
    # Isolate the secret-cache read path from the pre-existing digest-dirty
    # write path (also `get_conn`, exercised separately above) so this test
    # counts only secret lookups.
    monkeypatch.setattr(asana_webhook, "_mark_digest_dirty", lambda: None)
    reads = []

    def counting_conn():
        reads.append(1)
        return FakeConn(row={"secret": PROJECT_SECRET})

    monkeypatch.setattr(asana_webhook, "get_conn", counting_conn)
    clock = [1000.0]
    monkeypatch.setattr(asana_webhook, "_now", lambda: clock[0])
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    asana_webhook.receive(body, sig, "p-family")
    clock[0] += asana_webhook._SECRET_TTL_SECONDS + 1
    asana_webhook.receive(body, sig, "p-family")
    assert len(reads) == 2


def test_db_outage_with_a_cold_cache_rejects_rather_than_raises(monkeypatch):
    _capture(monkeypatch)

    def boom():
        raise RuntimeError("cloud sql unreachable")

    monkeypatch.setattr(asana_webhook, "get_conn", boom)
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-family") == ("", 401)


def test_handshake_stores_the_secret_for_a_project(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(asana_webhook, "get_conn", lambda: conn)
    token = webhook_registry.project_token(MANAGED_PROJECT)
    body, status, headers = asana_webhook.handshake("shh", MANAGED_PROJECT, token)
    assert status == 200
    assert headers["X-Hook-Secret"] == "shh"
    assert any("INSERT INTO asana_webhooks" in q for q, _ in conn.executed)


def test_handshake_without_a_project_still_echoes(monkeypatch):
    body, status, headers = asana_webhook.handshake("shh")
    assert (status, headers["X-Hook-Secret"]) == (200, "shh")


def test_handshake_fails_closed_when_the_secret_cannot_be_stored(monkeypatch):
    def boom():
        raise RuntimeError("cloud sql unreachable")

    monkeypatch.setattr(asana_webhook, "get_conn", boom)
    token = webhook_registry.project_token(MANAGED_PROJECT)
    assert asana_webhook.handshake("shh", MANAGED_PROJECT, token) == ("", 500)


def test_handshake_without_a_target_token_is_rejected_and_writes_nothing(monkeypatch):
    """The attack: POST /?project=<real gid> with an X-Hook-Secret of the
    attacker's choosing. Without the target's `t` it never reaches the DB."""
    conn = FakeConn()
    monkeypatch.setattr(asana_webhook, "get_conn", lambda: conn)
    assert asana_webhook.handshake("attacker-chosen", MANAGED_PROJECT) == ("", 401)
    assert conn.executed == []


def test_handshake_with_a_forged_target_token_is_rejected_and_writes_nothing(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(asana_webhook, "get_conn", lambda: conn)
    assert asana_webhook.handshake("attacker-chosen", MANAGED_PROJECT, "deadbeef") == ("", 401)
    assert conn.executed == []


def test_a_token_minted_with_another_key_is_rejected(monkeypatch):
    """The token is only as good as ASANA_ESCALATE_TOKEN, which the attacker
    does not have."""
    conn = FakeConn()
    monkeypatch.setattr(asana_webhook, "get_conn", lambda: conn)
    monkeypatch.setenv("ASANA_ESCALATE_TOKEN", "some-other-key")
    forged = webhook_registry.project_token(MANAGED_PROJECT)
    monkeypatch.setenv("ASANA_ESCALATE_TOKEN", ESCALATE_TOKEN)
    assert asana_webhook.handshake("attacker-chosen", MANAGED_PROJECT, forged) == ("", 401)
    assert conn.executed == []


def test_handshake_for_an_unmanaged_project_is_rejected_and_writes_nothing(monkeypatch):
    """Even with a valid token: we never register a target for a project that
    is not in the managed map."""
    conn = FakeConn()
    monkeypatch.setattr(asana_webhook, "get_conn", lambda: conn)
    token = webhook_registry.project_token("p-stranger")
    assert asana_webhook.handshake("shh", "p-stranger", token) == ("", 401)
    assert conn.executed == []


def test_a_delivery_for_an_unmanaged_project_never_reaches_the_database(monkeypatch):
    _capture(monkeypatch)

    def no_db():
        raise AssertionError("unmanaged project must not open a connection")

    monkeypatch.setattr(asana_webhook, "get_conn", no_db)
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-stranger") == ("", 401)

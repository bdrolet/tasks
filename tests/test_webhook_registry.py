import pytest

from services import webhook_registry as reg

BASE = "https://us-central1-x.cloudfunctions.net/tasks-webhook"
KEY = "escalate-bearer"


@pytest.fixture(autouse=True)
def _signing_key(monkeypatch):
    monkeypatch.setenv(reg._SIGNING_KEY_ENV, KEY)


def test_target_project_reads_the_query_parameter():
    assert reg.target_project(f"{BASE}?project=p1", BASE) == "p1"
    assert reg.target_project(f"{BASE}/?project=p1", BASE) == "p1"


def test_a_target_without_a_project_is_left_alone():
    """The legacy single-project webhook (D7) must survive reconciliation."""
    assert reg.target_project(BASE, BASE) is None


def test_someone_elses_webhook_is_left_alone():
    assert reg.target_project("https://example.com/hook?project=p1", BASE) is None
    assert reg.target_project("", BASE) is None


def test_a_sibling_cloud_function_on_the_same_host_is_not_misidentified_as_ours():
    """tasks-events and tasks-webhook share a host and differ only by path;
    a host-only match would misidentify tasks-events' webhook as ours."""
    sibling = "https://us-central1-x.cloudfunctions.net/tasks-events?project=p1"
    assert reg.target_project(sibling, BASE) is None


def test_a_trailing_slash_on_either_url_still_matches():
    assert reg.target_project(f"{BASE}?project=p1", f"{BASE}/") == "p1"
    assert reg.target_project(f"{BASE}/?project=p1", BASE) == "p1"


def test_scheme_and_host_case_do_not_affect_matching():
    # Uppercase only scheme + host, not path — paths stay case-sensitive.
    shouted = "HTTPS://US-CENTRAL1-X.CLOUDFUNCTIONS.NET/tasks-webhook"
    assert reg.target_project(f"{shouted}?project=p1", BASE) == "p1"


def test_a_differing_port_is_left_alone():
    ported = BASE.replace(
        "us-central1-x.cloudfunctions.net", "us-central1-x.cloudfunctions.net:8443"
    )
    assert reg.target_project(f"{ported}?project=p1", BASE) is None


def test_extra_query_parameters_alongside_project_are_ignored():
    assert reg.target_project(f"{BASE}?foo=bar&project=p1&baz=qux", BASE) == "p1"


def test_the_target_token_does_not_disturb_project_extraction():
    """target_project reads only `project`; the `t` parameter is the
    handshake's concern and must not change what reconciliation sees."""
    assert reg.target_project(reg.target_for(BASE, "p1"), BASE) == "p1"
    assert reg.target_project(f"{BASE}?t=abc&project=p1", BASE) == "p1"
    # And the legacy no-parameter target still reads as "not ours to manage".
    assert reg.target_project(BASE, BASE) is None


def test_target_for_carries_the_project_and_its_token():
    target = reg.target_for(BASE, "p1")
    assert target == f"{BASE}?project=p1&t={reg.project_token('p1')}"
    assert reg.token_valid("p1", reg.project_token("p1"))


def test_a_token_is_bound_to_one_project():
    assert not reg.token_valid("p2", reg.project_token("p1"))


def test_an_absent_or_wrong_token_is_invalid():
    assert not reg.token_valid("p1", None)
    assert not reg.token_valid("p1", "")
    assert not reg.token_valid("p1", "deadbeef")


def test_a_token_signed_with_another_key_is_invalid(monkeypatch):
    monkeypatch.setenv(reg._SIGNING_KEY_ENV, "other-key")
    forged = reg.project_token("p1")
    monkeypatch.setenv(reg._SIGNING_KEY_ENV, KEY)
    assert not reg.token_valid("p1", forged)


def test_without_a_signing_key_nothing_can_be_signed_or_verified(monkeypatch):
    monkeypatch.delenv(reg._SIGNING_KEY_ENV, raising=False)
    assert not reg.token_valid("p1", "anything")
    with pytest.raises(RuntimeError):
        reg.target_for(BASE, "p1")


def test_empty_state_registers_everything():
    p = reg.plan({"p1", "p2"}, {}, set())
    assert p.to_register == ["p1", "p2"]
    assert p.to_delete == []


def test_steady_state_does_nothing():
    p = reg.plan({"p1"}, {"p1": "w1"}, {"p1"})
    assert p == reg.Plan([], [])


def test_a_new_project_is_registered():
    p = reg.plan({"p1", "p2"}, {"p1": "w1"}, {"p1"})
    assert p.to_register == ["p2"]
    assert p.to_delete == []


def test_an_unmanaged_project_is_deregistered():
    p = reg.plan({"p1"}, {"p1": "w1", "p9": "w9"}, {"p1", "p9"})
    assert p.to_register == []
    assert p.to_delete == [("p9", "w9")]


def test_a_webhook_with_no_secret_row_is_replaced():
    p = reg.plan({"p1"}, {"p1": "w1"}, set())
    assert p.to_delete == [("p1", "w1")]
    assert p.to_register == ["p1"]


def test_an_inactive_webhook_is_replaced():
    """Asana marks a webhook active: false once deliveries keep failing. It
    still exists and its target still parses, but it delivers nothing."""
    p = reg.plan({"p1"}, {"p1": "w1"}, {"p1"}, inactive={"p1"})
    assert p.to_delete == [("p1", "w1")]
    assert p.to_register == ["p1"]


def test_an_inactive_webhook_is_replaced_exactly_once():
    """Inactive AND missing its secret row is still one delete, one register."""
    p = reg.plan({"p1"}, {"p1": "w1"}, set(), inactive={"p1"})
    assert p.to_delete == [("p1", "w1")]
    assert p.to_register == ["p1"]


def test_an_inactive_webhook_for_an_unmanaged_project_is_simply_deleted():
    p = reg.plan({"p1"}, {"p1": "w1", "p9": "w9"}, {"p1", "p9"}, inactive={"p9"})
    assert p.to_delete == [("p9", "w9")]
    assert p.to_register == []


def test_inactive_defaults_to_empty():
    """The parameter is additive: existing three-argument callers are steady."""
    assert reg.plan({"p1"}, {"p1": "w1"}, {"p1"}) == reg.Plan([], [])

from services import webhook_registry as reg

BASE = "https://us-central1-x.cloudfunctions.net/tasks-webhook"


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

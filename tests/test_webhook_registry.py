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

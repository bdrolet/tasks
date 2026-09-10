import json

from services import managed_projects as mp


def test_parses_the_map(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, json.dumps({"p1": {"done": "s1"}, "p2": {"done": None}}))
    assert mp.managed() == {"p1": {"done": "s1"}, "p2": {"done": None}}
    assert mp.gids() == {"p1", "p2"}
    assert mp.done_section("p1") == "s1"
    assert mp.done_section("p2") is None
    assert mp.done_section("p3") is None
    assert mp.done_section(None) is None


def test_unset_map_is_empty(monkeypatch, caplog):
    monkeypatch.delenv(mp.ENV_VAR, raising=False)
    assert mp.managed() == {}
    assert mp.gids() == set()
    assert any(mp.ENV_VAR in record.message and record.levelname == "WARNING" for record in caplog.records)


def test_malformed_map_degrades_to_empty(monkeypatch, caplog):
    monkeypatch.setenv(mp.ENV_VAR, "{not json")
    caplog.clear()
    assert mp.managed() == {}
    assert any(mp.ENV_VAR in record.message and record.levelname == "WARNING" for record in caplog.records)

    monkeypatch.setenv(mp.ENV_VAR, '["p1"]')
    caplog.clear()
    assert mp.managed() == {}
    assert any(mp.ENV_VAR in record.message and record.levelname == "WARNING" for record in caplog.records)

    monkeypatch.setenv(mp.ENV_VAR, '{"p1": "s1"}')
    assert mp.managed() == {"p1": {"done": None}}


def test_project_of_prefers_a_managed_membership(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, json.dumps({"p2": {"done": "s2"}}))
    task = {
        "memberships": [
            {"project": {"gid": "p9"}},
            {"project": {"gid": "p2"}},
        ]
    }
    assert mp.project_of(task) == "p2"


def test_project_of_falls_back_to_the_first_membership(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of({"memberships": [{"project": {"gid": "p9"}}]}) == "p9"


def test_project_of_a_subtask_is_none(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of({"memberships": []}) is None
    assert mp.project_of({}) is None

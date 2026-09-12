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
    assert any(
        mp.ENV_VAR in record.message and record.levelname == "WARNING" for record in caplog.records
    )


def test_malformed_map_degrades_to_empty(monkeypatch, caplog):
    monkeypatch.setenv(mp.ENV_VAR, "{not json")
    caplog.clear()
    assert mp.managed() == {}
    assert any(
        mp.ENV_VAR in record.message and record.levelname == "WARNING" for record in caplog.records
    )

    monkeypatch.setenv(mp.ENV_VAR, '["p1"]')
    caplog.clear()
    assert mp.managed() == {}
    assert any(
        mp.ENV_VAR in record.message and record.levelname == "WARNING" for record in caplog.records
    )

    monkeypatch.setenv(mp.ENV_VAR, '{"p1": "s1"}')
    assert mp.managed() == {"p1": {"done": None}}


def _multi_homed(*gids):
    return {"memberships": [{"project": {"gid": gid}} for gid in gids]}


def test_project_of_prefers_a_managed_membership(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv(mp.ENV_VAR, json.dumps({"p2": {"done": "s2"}}))
    assert mp.project_of(_multi_homed("p9", "p2")) == "p2"


def test_project_of_falls_back_to_the_first_membership(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of(_multi_homed("p9")) == "p9"


def test_project_of_a_subtask_is_none(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of({"memberships": []}) is None
    assert mp.project_of({}) is None


def test_project_of_an_empty_map_still_resolves_to_the_default_project(monkeypatch):
    """The whole rollout rests on "with the map empty, nothing changes". A
    multi-homed task must resolve to the default project whatever order Asana
    happened to list its memberships in — otherwise sections.done() returns
    None for it and the Done move is silently skipped."""
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of(_multi_homed("p-family", "p-ben")) == "p-ben"
    assert mp.project_of(_multi_homed("p-ben", "p-family")) == "p-ben"


def test_project_of_prefers_the_default_project_over_another_managed_one(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv(
        mp.ENV_VAR, json.dumps({"p-family": {"done": "sec-family"}, "p-ben": {"done": "sec-ben"}})
    )
    assert mp.project_of(_multi_homed("p-family", "p-ben")) == "p-ben"


def test_project_of_resolves_two_managed_projects_by_declaration_order(monkeypatch):
    """Asana returns memberships in no guaranteed order, so the map's own key
    order — not the task's — decides which Done section a multi-homed task
    gets. Same membership set, both orderings, one answer."""
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv(
        mp.ENV_VAR,
        json.dumps({"p-family": {"done": "sec-family"}, "p-carter": {"done": "sec-carter"}}),
    )
    assert mp.project_of(_multi_homed("p-carter", "p-family")) == "p-family"
    assert mp.project_of(_multi_homed("p-family", "p-carter")) == "p-family"

    # Reversing the map reverses the answer — the declaration is what decides.
    monkeypatch.setenv(
        mp.ENV_VAR,
        json.dumps({"p-carter": {"done": "sec-carter"}, "p-family": {"done": "sec-family"}}),
    )
    assert mp.project_of(_multi_homed("p-family", "p-carter")) == "p-carter"
    assert mp.project_of(_multi_homed("p-carter", "p-family")) == "p-carter"


def test_project_of_ignores_an_unset_default_project(monkeypatch):
    monkeypatch.delenv("ASANA_PROJECT_ID", raising=False)
    monkeypatch.setenv(mp.ENV_VAR, json.dumps({"p-family": {"done": "sec-family"}}))
    assert mp.project_of(_multi_homed("p9", "p-family")) == "p-family"

from datetime import date

import clients.asana as asana
from services import custom_fields as cf

FIELDS = [
    {"gid": "cf-points", "name": "Story points", "resource_subtype": "number"},
    {"gid": "cf-started", "name": "Started at", "resource_subtype": "date"},
    {"gid": "cf-other", "name": "Estimated time", "resource_subtype": "number"},
]


def _fields(monkeypatch):
    calls = []
    monkeypatch.setattr(asana, "list_custom_fields", lambda: (calls.append(1), list(FIELDS))[1])
    cf.gids(refresh=True)
    return calls


def test_gids_resolve_by_name_and_cache(monkeypatch):
    calls = _fields(monkeypatch)
    assert cf.gids() == {"Story points": "cf-points", "Started at": "cf-started"}
    cf.gids()
    cf.gids()
    assert len(calls) == 1


def test_read_values_from_task(monkeypatch):
    _fields(monkeypatch)
    task = {
        "custom_fields": [
            {"gid": "cf-points", "name": "Story points", "number_value": 3.0},
            {"gid": "cf-started", "name": "Started at", "date_value": {"date": "2026-09-22"}},
        ]
    }
    assert cf.read(task) == (3, date(2026, 9, 22))
    assert cf.read({"custom_fields": []}) == (None, None)
    assert cf.read({}) == (None, None)


def test_setters_send_the_field_map(monkeypatch):
    _fields(monkeypatch)
    sent = []
    monkeypatch.setattr(asana, "update_task", lambda gid, fields: sent.append((gid, fields)))
    cf.set_story_points("t1", 5)
    cf.set_started_at("t1", date(2026, 9, 23))
    cf.set_started_at("t1", None)
    assert sent == [
        ("t1", {"custom_fields": {"cf-points": 5}}),
        ("t1", {"custom_fields": {"cf-started": "2026-09-23"}}),
        ("t1", {"custom_fields": {"cf-started": None}}),
    ]


def test_ensure_creates_missing_and_attaches(monkeypatch):
    existing = [FIELDS[2]]
    created, attached = [], []
    monkeypatch.setattr(asana, "list_custom_fields", lambda: list(existing))

    def create(name, subtype, *, precision=None):
        f = {"gid": f"cf-{name}", "name": name, "resource_subtype": subtype}
        existing.append(f)
        created.append((name, subtype, precision))
        return f

    monkeypatch.setattr(asana, "create_custom_field", create)
    monkeypatch.setattr(asana, "add_custom_field_to_project", lambda p, f: attached.append((p, f)))
    out = cf.ensure(["p-a", "p-b"])
    assert created == [("Story points", "number", 0), ("Started at", "date", None)]
    assert set(attached) == {
        ("p-a", "cf-Story points"),
        ("p-a", "cf-Started at"),
        ("p-b", "cf-Story points"),
        ("p-b", "cf-Started at"),
    }
    assert out == {"Story points": "cf-Story points", "Started at": "cf-Started at"}

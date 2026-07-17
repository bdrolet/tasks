import clients.asana as asana
from handlers import label_applied
from models.events import LabelAppliedEvent
from repo import tasks as repo_tasks
from tests.test_repo import FakeConn


def make_event(**overrides) -> LabelAppliedEvent:
    event: LabelAppliedEvent = {
        "event": "label_applied",
        "message_id": "msg-123",
        "task_gid": "42",
        "label": "respond",
        "source": "human_correction",
    }
    event.update(overrides)  # type: ignore[typeddict-item]
    return event


def _stub_task(monkeypatch, section_name="Review"):
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid})
    monkeypatch.setattr(
        asana, "current_section", lambda task: {"gid": "s-old", "name": section_name}
    )


def _stub_db(monkeypatch, gid=None):
    monkeypatch.setattr(label_applied, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(repo_tasks, "get_gid_by_message", lambda conn, mid: gid)


def test_moves_task_to_label_section(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_RESPOND_GID", "sec-respond")
    _stub_task(monkeypatch)
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    label_applied.handle(make_event())
    assert moves == [("42", "sec-respond")]


def test_missing_task_gid_resolves_from_db(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_RESPOND_GID", "sec-respond")
    _stub_task(monkeypatch)
    _stub_db(monkeypatch, gid="55")
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    label_applied.handle(make_event(task_gid=None))
    assert moves == [("55", "sec-respond")]


def test_db_miss_falls_back_to_external_lookup(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_RESPOND_GID", "sec-respond")
    _stub_task(monkeypatch)
    _stub_db(monkeypatch, gid=None)
    monkeypatch.setattr(asana, "find_task_by_external", lambda mid: "77")
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    label_applied.handle(make_event(task_gid=None))
    assert moves == [("77", "sec-respond")]


def test_no_task_found_is_a_noop(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_RESPOND_GID", "sec-respond")
    _stub_db(monkeypatch, gid=None)
    monkeypatch.setattr(asana, "find_task_by_external", lambda mid: None)
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    label_applied.handle(make_event(task_gid=None))
    assert moves == []


def test_unmapped_label_is_a_noop(monkeypatch):
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    label_applied.handle(make_event(label="ignore"))
    assert moves == []

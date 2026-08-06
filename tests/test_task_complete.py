import clients.asana as asana
from handlers import task_complete
from tests.test_repo import FakeConn


def test_completed_task_moves_to_done(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    db = FakeConn()
    monkeypatch.setattr(task_complete, "get_conn", lambda: db)
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": True})
    monkeypatch.setattr(
        asana, "current_section", lambda task: {"gid": "s-review", "name": "Review"}
    )
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_complete.handle("42")
    assert moves == [("42", "sec-done")]
    assert any("completed_at" in q for q, _ in db.executed)


def test_incomplete_task_is_ignored(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": False})
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_complete.handle("42")
    assert moves == []


def test_complete_updates_index(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": True})
    monkeypatch.setattr(asana, "current_section", lambda task: None)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    calls = []
    monkeypatch.setattr(
        task_complete.repo_index,
        "set_completed",
        lambda conn, gid, done: calls.append((gid, done)),
    )
    task_complete.handle("42")
    assert calls == [("42", True)]


def test_uncomplete_clears_index_flag(monkeypatch):
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": False})
    calls = []
    monkeypatch.setattr(
        task_complete.repo_index,
        "set_completed",
        lambda conn, gid, done: calls.append((gid, done)),
    )
    task_complete.handle("42")
    assert calls == [("42", False)]


def test_already_in_done_is_a_noop(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": True})
    monkeypatch.setattr(asana, "current_section", lambda task: {"gid": "sec-done", "name": "Done"})
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_complete.handle("42")
    assert moves == []

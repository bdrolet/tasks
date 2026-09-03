from dateutil.relativedelta import relativedelta

import clients.asana as asana
from handlers import task_complete
from services import recurrence
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


def _wire_completion(monkeypatch, task):
    """The minimum stubs for handle() to reach the Done move."""
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: task)
    monkeypatch.setattr(asana, "current_section", lambda t: {"gid": "s-review", "name": "Review"})
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))
    return moves


def test_repeat_tag_spawns_the_next_occurrence(monkeypatch):
    task = {
        "gid": "42",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
        "tags": [{"gid": "t2", "name": "repeat:3mo"}],
    }
    moves = _wire_completion(monkeypatch, task)
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: {"name": "x", "tags": []})
    spawned = []
    monkeypatch.setattr(
        recurrence,
        "spawn_next",
        lambda t, d, s, rule: spawned.append((t["gid"], s, rule)) or "new-1",
    )

    task_complete.handle("42")

    assert spawned == [
        ("42", {"gid": "s-review", "name": "Review"}, ("t2", relativedelta(months=3)))
    ]
    assert moves == [("42", "sec-done")]  # the Done move still happens


def test_completion_without_a_repeat_tag_touches_no_recurrence_code(monkeypatch):
    task = {"gid": "42", "completed": True, "tags": [{"gid": "t1", "name": "home"}]}
    moves = _wire_completion(monkeypatch, task)
    # A raising stub would be swallowed by the handler's `except Exception` —
    # record the call instead, so the assertion runs outside that guard.
    detail_calls: list[str] = []
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: detail_calls.append(gid) or {})

    task_complete.handle("42")

    assert detail_calls == []
    assert moves == [("42", "sec-done")]


def test_a_failing_find_rule_still_completes_and_moves_the_task(monkeypatch):
    # find_rule itself must be inside the guard, not just spawn_next — it
    # indexes into Asana-supplied tag dicts (candidates[0]["gid"]) and can
    # raise on its own, before spawn_next is ever reached.
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    db = FakeConn()
    monkeypatch.setattr(task_complete, "get_conn", lambda: db)
    task = {
        "gid": "42",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
        "tags": [{"gid": "t2", "name": "repeat:3mo"}],
    }
    monkeypatch.setattr(asana, "get_task", lambda gid: task)
    monkeypatch.setattr(asana, "current_section", lambda t: {"gid": "s-review", "name": "Review"})
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    def boom(tags):
        raise KeyError("gid")  # e.g. a repeat tag Asana returned without one

    monkeypatch.setattr(recurrence, "find_rule", boom)

    task_complete.handle("42")

    assert moves == [("42", "sec-done")]
    assert any("completed_at" in q for q, _ in db.executed)


def test_a_failing_recurrence_still_completes_and_moves_the_task(monkeypatch):
    task = {
        "gid": "42",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
        "tags": [{"gid": "t2", "name": "repeat:3mo"}],
    }
    moves = _wire_completion(monkeypatch, task)
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: {"name": "x", "tags": []})

    def boom(*a, **k):
        raise RuntimeError("asana down")

    monkeypatch.setattr(recurrence, "spawn_next", boom)

    task_complete.handle("42")
    assert moves == [("42", "sec-done")]

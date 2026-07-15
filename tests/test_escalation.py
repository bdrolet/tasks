import clients.asana as asana
from repo import tasks as repo_tasks
from services import escalation
from tests.test_repo import FakeConn


def _tasks(monkeypatch, tasks):
    monkeypatch.setattr(asana, "get_incomplete_tasks_past_due", lambda project_gid=None: tasks)


def _stub_db(monkeypatch, escalated_gids=()):
    monkeypatch.setattr(escalation, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(repo_tasks, "was_escalated", lambda conn, gid: gid in escalated_gids)
    marked = []
    monkeypatch.setattr(repo_tasks, "mark_escalated", lambda conn, gid: marked.append(gid))
    return marked


def test_moves_overdue_tasks(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_OVERDUE_GID", "sec-overdue")
    monkeypatch.delenv("ASANA_OVERDUE_TAG_GID", raising=False)
    _tasks(monkeypatch, [{"gid": "1", "memberships": []}, {"gid": "2", "memberships": []}])
    marked = _stub_db(monkeypatch)
    monkeypatch.setattr(asana, "current_section", lambda task: None)
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    result = escalation.run()
    assert moves == [("1", "sec-overdue"), ("2", "sec-overdue")]
    assert marked == ["1", "2"]
    assert result == {"scanned": 2, "escalated": 2}


def test_skips_previously_escalated_per_db(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_OVERDUE_GID", "sec-overdue")
    monkeypatch.delenv("ASANA_OVERDUE_TAG_GID", raising=False)
    _tasks(monkeypatch, [{"gid": "1", "memberships": []}])
    _stub_db(monkeypatch, escalated_gids=("1",))
    monkeypatch.setattr(asana, "current_section", lambda task: None)
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    result = escalation.run()
    assert moves == []
    assert result == {"scanned": 1, "escalated": 0}


def test_skips_tasks_already_in_overdue(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_OVERDUE_GID", "sec-overdue")
    monkeypatch.delenv("ASANA_OVERDUE_TAG_GID", raising=False)
    _tasks(monkeypatch, [{"gid": "1", "memberships": []}])
    _stub_db(monkeypatch)
    monkeypatch.setattr(
        asana, "current_section", lambda task: {"gid": "sec-overdue", "name": "Overdue"}
    )
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    result = escalation.run()
    assert moves == []
    assert result == {"scanned": 1, "escalated": 0}


def test_tags_when_tag_gid_configured(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_OVERDUE_GID", "sec-overdue")
    monkeypatch.setenv("ASANA_OVERDUE_TAG_GID", "tag-overdue")
    _tasks(monkeypatch, [{"gid": "1", "memberships": []}])
    _stub_db(monkeypatch)
    monkeypatch.setattr(asana, "current_section", lambda task: None)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    tagged = []
    monkeypatch.setattr(asana, "add_tag", lambda t, tag: tagged.append((t, tag)))

    escalation.run()
    assert tagged == [("1", "tag-overdue")]


def test_unconfigured_returns_zero(monkeypatch):
    monkeypatch.delenv("ASANA_SECTION_OVERDUE_GID", raising=False)
    monkeypatch.delenv("ASANA_OVERDUE_TAG_GID", raising=False)
    assert escalation.run() == {"scanned": 0, "escalated": 0}

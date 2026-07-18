from services import task_search


def _task(gid, name="Task", notes="", completed=False, due_on=None):
    return {"gid": gid, "name": name, "notes": notes, "completed": completed, "due_on": due_on}


def test_resolve_project_by_gid_and_name():
    projects = [{"gid": "p1", "name": "Inbox"}, {"gid": "p2", "name": "Chores"}]
    assert task_search.resolve_project(projects, "p2")["gid"] == "p2"
    assert task_search.resolve_project(projects, "chores")["gid"] == "p2"
    assert task_search.resolve_project(projects, "nope") is None


def test_filter_matches_name_and_notes_case_insensitive():
    tasks = [
        _task("t1", name="Renew PASSPORT"),
        _task("t2", notes="passport photos needed"),
        _task("t3", name="Unrelated"),
    ]
    got = task_search.filter_tasks(
        tasks, query="passport", completed=None, due_before=None, due_after=None
    )
    assert {t["gid"] for t in got} == {"t1", "t2"}


def test_filter_empty_query_matches_all():
    got = task_search.filter_tasks(
        [_task("t1"), _task("t2")], query="", completed=None, due_before=None, due_after=None
    )
    assert len(got) == 2


def test_filter_dedupes_by_gid():
    got = task_search.filter_tasks(
        [_task("t1"), _task("t1")], query="", completed=None, due_before=None, due_after=None
    )
    assert len(got) == 1


def test_filter_completed_flag():
    tasks = [_task("t1", completed=True), _task("t2", completed=False)]
    open_only = task_search.filter_tasks(
        tasks, query="", completed=False, due_before=None, due_after=None
    )
    assert [t["gid"] for t in open_only] == ["t2"]


def test_filter_due_bounds_inclusive_and_drop_undated():
    tasks = [
        _task("t1", due_on="2026-07-01"),
        _task("t2", due_on="2026-07-15"),
        _task("t3", due_on=None),
    ]
    got = task_search.filter_tasks(
        tasks, query="", completed=None, due_before="2026-07-15", due_after="2026-07-01"
    )
    assert {t["gid"] for t in got} == {"t1", "t2"}


def test_sort_due_date_asc_nulls_last():
    tasks = [
        _task("t1", due_on=None, name="b"),
        _task("t2", due_on="2026-08-01"),
        _task("t3", due_on="2026-07-01"),
    ]
    got = task_search.filter_tasks(tasks, query="", completed=None, due_before=None, due_after=None)
    assert [t["gid"] for t in got] == ["t3", "t2", "t1"]


def test_snippet_extracts_around_match():
    notes = "x" * 100 + "the PASSPORT expires" + "y" * 100
    s = task_search.snippet(notes, "passport")
    assert "PASSPORT" in s
    assert s.startswith("…") and s.endswith("…")
    assert task_search.snippet(notes, "absent") is None
    assert task_search.snippet(None, "q") is None

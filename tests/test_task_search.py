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


def test_summary_prefers_lead_context_over_key_points():
    notes = (
        "Front-fascia parts still to purchase for the bumper job.\n"
        "Key points:    Bumper cover already ordered\n    One subtask per part\n"
        "Source: Created manually"
    )
    assert task_search.summary(notes) == (
        "Front-fascia parts still to purchase for the bumper job."
    )


def test_summary_falls_back_to_first_key_point():
    notes = (
        "Key points:    Seller agreed to cancel the order\n"
        "    Refund not yet posted\n"
        "Links:    https://example.com/x\n"
        "Source: Email    From: eBay (ebay@ebay.com)"
    )
    assert task_search.summary(notes) == "Seller agreed to cancel the order"


def test_summary_reads_key_point_off_the_following_line():
    notes = "Key points:\n    Renew before the policy lapses\nSource: Created manually"
    assert task_search.summary(notes) == "Renew before the policy lapses"


def test_summary_clips_at_a_word_boundary():
    notes = " ".join(["alpha"] * 60)
    got = task_search.summary(notes, limit=40)
    assert got.endswith("…") and len(got) <= 41
    assert "alph…" not in got  # never splits a word


def test_summary_keeps_prose_that_merely_opens_with_a_header_word():
    # "Actions" carries no colon in the rendered description, so a bare prefix
    # match would truncate this to nothing.
    notes = "Actions agreed with Mo are on hold until the fascia lands.\nSource: Created manually"
    assert task_search.summary(notes).startswith("Actions agreed with Mo")


def test_summary_none_when_there_is_nothing_but_boilerplate():
    assert task_search.summary("Source: Created manually") is None
    assert task_search.summary("") is None
    assert task_search.summary(None) is None

from datetime import date, datetime, timezone

from models.digest import DigestTask
from services import due_digest as dd

ROUTE = dict(
    family_project_gid="fam", family_calendar_id="cal-fam", shared_calendar_id="cal-shared"
)


def _task(
    gid="1",
    name="[P1] Do thing",
    due_on="2026-09-10",
    calendar_id="primary",
    points=None,
    links=None,
):
    return DigestTask(
        gid=gid,
        name=name,
        permalink_url=f"https://app.asana.com/0/0/{gid}",
        due_on=due_on,
        calendar_id=calendar_id,
        points=points or [],
        links=links or [],
    )


def test_today_local_uses_los_angeles():
    # 2026-09-10T03:00Z is still 2026-09-09 in Los Angeles (UTC-7).
    assert dd.today_local(datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)) == date(2026, 9, 9)


def test_route_family_project_wins_over_tag():
    task = {"memberships": [{"project": {"gid": "fam"}}], "tags": [{"name": "cheryl"}]}
    assert dd.route(task, **ROUTE) == "cal-fam"


def test_route_cheryl_tag_case_insensitive():
    task = {"memberships": [{"project": {"gid": "other"}}], "tags": [{"name": "Cheryl"}]}
    assert dd.route(task, **ROUTE) == "cal-shared"


def test_route_default_primary():
    task = {"memberships": [{"project": {"gid": "other"}}], "tags": []}
    assert dd.route(task, **ROUTE) == dd.PRIMARY


def test_route_skips_rules_whose_env_is_missing():
    task = {"memberships": [{"project": {"gid": "fam"}}], "tags": [{"name": "cheryl"}]}
    assert (
        dd.route(task, family_project_gid="", family_calendar_id="", shared_calendar_id="")
        == "primary"
    )
    assert (
        dd.route(task, family_project_gid="", family_calendar_id="", shared_calendar_id="s") == "s"
    )


def test_in_window_edges():
    today = date(2026, 9, 1)
    assert dd.in_window({"due_on": "2026-09-01", "completed": False}, today)
    assert dd.in_window({"due_on": "2026-10-01", "completed": False}, today)  # day 30
    assert not dd.in_window({"due_on": "2026-10-02", "completed": False}, today)  # day 31
    assert not dd.in_window({"due_on": "2026-08-31", "completed": False}, today)
    assert not dd.in_window({"due_on": None, "completed": False}, today)
    assert not dd.in_window({"due_on": "2026-09-05", "completed": True}, today)


def test_order_by_priority_prefix_then_name():
    tasks = [
        _task("a", "zeta"),
        _task("b", "[P3] beta"),
        _task("c", "[P0] Alpha"),
        _task("d", "[P0] alpha2"),
        _task("e", "Beta"),
    ]
    assert [t.gid for t in dd.order(tasks)] == ["c", "d", "b", "e", "a"]


def test_title_for_pluralizes():
    assert dd.title_for(1) == "1 task due"
    assert dd.title_for(3) == "3 tasks due"


def test_build_events_groups_by_day_and_calendar():
    tasks = [
        _task(
            "1", "[P1] A", "2026-09-10", "primary", points=["p1"], links=[("https://d/1", "Doc")]
        ),
        _task("2", "[P0] B", "2026-09-10", "primary"),
        _task("3", "[P2] C", "2026-09-10", "cal-fam"),
        _task("4", "[P1] D", "2026-09-11", "primary"),
    ]
    events = dd.build_events(tasks)
    assert set(events) == {
        ("2026-09-10", "primary"),
        ("2026-09-10", "cal-fam"),
        ("2026-09-11", "primary"),
    }
    ev = events[("2026-09-10", "primary")]
    assert ev.title == "2 tasks due"
    assert ev.task_gids == ["2", "1"]
    assert ev.sections[1] == {
        "title": "[P1] A",
        "url": "https://app.asana.com/0/0/1",
        "points": ["p1"],
        "links": [["https://d/1", "Doc"]],
    }
    assert ev.content_hash() == events[("2026-09-10", "primary")].content_hash()
    assert ev.content_hash() != events[("2026-09-11", "primary")].content_hash()


def _row(day, cal="primary", event_id="e1", content_hash="h"):
    return {
        "day": day,
        "calendar_id": cal,
        "event_id": event_id,
        "content_hash": content_hash,
        "task_gids": [],
    }


def test_plan_creates_updates_deletes_and_leaves_past_alone():
    today = date(2026, 9, 10)
    desired = dd.build_events(
        [_task("1", "[P1] A", "2026-09-10"), _task("2", "[P1] B", "2026-09-12")]
    )
    same = desired[("2026-09-12", "primary")]
    stored = [
        _row("2026-09-12", content_hash=same.content_hash(), event_id="keep"),  # unchanged
        _row("2026-09-11", event_id="gone"),  # in window, no tasks → delete
        _row("2026-09-01", event_id="past"),  # past → untouched
    ]
    p = dd.plan(desired, stored, today)
    assert [e.day for e in p.creates] == ["2026-09-10"]
    assert p.updates == []
    assert [r["event_id"] for r in p.deletes] == ["gone"]


def test_plan_updates_when_hash_differs():
    today = date(2026, 9, 10)
    desired = dd.build_events([_task("1", "[P1] A", "2026-09-10")])
    stored = [_row("2026-09-10", content_hash="stale", event_id="e9")]
    p = dd.plan(desired, stored, today)
    assert p.creates == [] and p.deletes == []
    assert p.updates[0][0].day == "2026-09-10" and p.updates[0][1]["event_id"] == "e9"

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import clients.asana as asana
import clients.schedule_api as sapi
from handlers import due_digest as h
from repo import due_digest as repo
from services import task_bullets as tb
from tests.test_repo_due_digest import RowsConn

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)  # 08:00 Los Angeles → today = 2026-09-10


def test_should_rebuild_matrix():
    rebuilt = NOW - timedelta(minutes=5)
    assert h.should_rebuild({"dirty_at": NOW, "last_rebuilt_at": rebuilt}, NOW, False)
    assert not h.should_rebuild(
        {"dirty_at": rebuilt - timedelta(minutes=1), "last_rebuilt_at": rebuilt}, NOW, False
    )
    assert h.should_rebuild({"dirty_at": None, "last_rebuilt_at": None}, NOW, False)
    assert h.should_rebuild(
        {"dirty_at": None, "last_rebuilt_at": NOW - timedelta(minutes=61)}, NOW, False
    )
    assert not h.should_rebuild(
        {"dirty_at": None, "last_rebuilt_at": NOW - timedelta(minutes=59)}, NOW, False
    )
    assert h.should_rebuild({"dirty_at": None, "last_rebuilt_at": rebuilt}, NOW, True)


def test_project_calendars_sorts_by_order_and_drops_calendarless(monkeypatch):
    # Keys are deliberately not in `order` sequence, and "boardless" would sort
    # first by both key order and `order` if it were not dropped for having no
    # calendar — a regression to key order fails this test.
    monkeypatch.setenv(
        "ASANA_PROJECT_CALENDARS",
        json.dumps(
            {
                "carter": {"calendar": "cal-shared", "order": 20},
                "boardless": {"order": 5},
                "fam": {"calendar": "cal-fam", "order": 10},
            }
        ),
    )
    assert h._project_calendars() == [("fam", "cal-fam"), ("carter", "cal-shared")]


def test_project_calendars_defaults_order_and_ties_break_by_gid(monkeypatch):
    monkeypatch.setenv(
        "ASANA_PROJECT_CALENDARS",
        json.dumps(
            {
                "zeta": {"calendar": "cal-z"},
                "alpha": {"calendar": "cal-a"},
                "fam": {"calendar": "cal-fam", "order": 10},
            }
        ),
    )
    assert h._project_calendars() == [
        ("fam", "cal-fam"),
        ("alpha", "cal-a"),
        ("zeta", "cal-z"),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json",
        "[]",
        '{"fam": "cal-fam"}',
        '{"fam": {"calendar": "cal-fam", "order": "soon"}}',
    ],
)
def test_project_calendars_unset_or_malformed_yields_no_rules(monkeypatch, raw):
    monkeypatch.setenv("ASANA_PROJECT_CALENDARS", raw)
    assert h._project_calendars() == []


def test_routing_returns_routes_kwargs(env):
    assert h._routing() == {
        "project_calendars": [("fam", "cal-fam"), ("cheryl", "cal-shared")],
        "shared_calendar_id": "cal-shared",
    }


def test_routing_without_shared_calendar_keeps_project_rules(env, monkeypatch):
    monkeypatch.delenv("CALENDAR_SHARED_ID")
    routing = h._routing()
    assert routing["shared_calendar_id"] == ""
    assert routing["project_calendars"] == [("fam", "cal-fam"), ("cheryl", "cal-shared")]


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SCHEDULE_API_URL", "https://s")
    monkeypatch.setenv("SCHEDULE_API_TOKEN", "t")
    monkeypatch.setenv(
        "ASANA_PROJECT_CALENDARS",
        json.dumps(
            {
                "fam": {"calendar": "cal-fam", "order": 10},
                "cheryl": {"calendar": "cal-shared", "order": 20},
            }
        ),
    )
    monkeypatch.setenv("CALENDAR_SHARED_ID", "cal-shared")
    monkeypatch.setattr(h, "_now", lambda: NOW)


def _asana(monkeypatch, tasks):
    monkeypatch.setattr(
        asana,
        "list_projects",
        lambda: [{"gid": "p1", "name": "Work"}, {"gid": "fam", "name": "Family Board"}],
    )
    monkeypatch.setattr(
        asana,
        "list_project_tasks",
        lambda gid, only_open=False, opt_fields=None: [t for t in tasks if t["_project"] == gid],
    )
    monkeypatch.setattr(
        asana, "list_my_tasks", lambda only_open=False, opt_fields=None: list(tasks)
    )


def _task(gid, name, due_on, project="p1", tags=(), notes="<body>x</body>"):
    return {
        "gid": gid,
        "name": name,
        "due_on": due_on,
        "completed": False,
        "permalink_url": f"https://app.asana.com/0/0/{gid}",
        "html_notes": notes,
        "tags": [{"name": t} for t in tags],
        "memberships": [{"project": {"gid": project}}],
        "_project": project,
    }


class Store:
    """In-memory stand-in for repo.due_digest, patched function by function."""

    def __init__(self, rows=None, state=None):
        self.rows = {(r["day"], r["calendar_id"]): r for r in (rows or [])}
        self.state = state or {"dirty_at": NOW, "last_rebuilt_at": None}
        self.bullets = {}
        self.rebuilt = 0
        self.rebuilt_at = None

    def patch(self, monkeypatch):
        monkeypatch.setattr(h, "get_conn", lambda: RowsConn())
        monkeypatch.setattr(repo, "get_state", lambda conn: self.state)
        monkeypatch.setattr(repo, "list_events", lambda conn, since: list(self.rows.values()))
        monkeypatch.setattr(
            repo,
            "upsert_event",
            lambda conn, **kw: self.rows.__setitem__((kw["day"], kw["calendar_id"]), kw),
        )
        monkeypatch.setattr(
            repo,
            "delete_event",
            lambda conn, day, calendar_id: self.rows.pop((day, calendar_id), None),
        )
        monkeypatch.setattr(repo, "prune_events", lambda conn, before: None)
        monkeypatch.setattr(repo, "get_bullets", lambda conn, gid: self.bullets.get(gid))
        monkeypatch.setattr(
            repo,
            "put_bullets",
            lambda conn, gid, ch, b: self.bullets.__setitem__(
                gid, {"content_hash": ch, "bullets": b}
            ),
        )

        def mark(at=None):
            self.rebuilt += 1
            self.rebuilt_at = at

        monkeypatch.setattr(repo, "mark_rebuilt", lambda conn, at=None: mark(at))


class Cal:
    def __init__(self, patch_404=False, search_hits=None):
        self.created, self.patched, self.deleted = [], [], []
        self.patch_404 = patch_404
        self.search_hits = search_hits or []
        self.n = 0

    def patch(self, monkeypatch):
        def create(**kw):
            self.n += 1
            self.created.append(kw)
            return {"event_id": f"new{self.n}", "calendar_id": kw["calendar"]}

        def patch_event(event_id, **kw):
            if self.patch_404:
                raise sapi.NotFound(event_id)
            self.patched.append((event_id, kw))
            return {}

        monkeypatch.setattr(sapi, "create_event", create)
        monkeypatch.setattr(sapi, "patch_event", patch_event)
        monkeypatch.setattr(
            sapi, "delete_event", lambda event_id, **kw: self.deleted.append(event_id)
        )
        monkeypatch.setattr(sapi, "search_digest_events", lambda **kw: list(self.search_hits))


def test_skipped_when_clean_and_fresh(env, monkeypatch):
    store = Store(state={"dirty_at": None, "last_rebuilt_at": NOW - timedelta(minutes=5)})
    store.patch(monkeypatch)
    assert h.run() == {"outcome": "skipped"}


def test_db_unavailable_skips(env, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(h, "get_conn", boom)
    assert h.run()["outcome"] == "db_unavailable"


def test_rebuild_creates_routed_events_and_records_rows(env, monkeypatch):
    _asana(
        monkeypatch,
        [
            _task("1", "[P1] Work thing", "2026-09-10"),
            _task("2", "[P0] Family thing", "2026-09-10", project="fam"),
            _task("3", "[P2] Shared thing", "2026-09-12", tags=("cheryl",)),
            _task("4", "[P1] Far away", "2026-12-01"),
        ],
    )
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "ok"))
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)

    out = h.run()
    assert out["outcome"] == "ok" and out["tasks"] == 3 and out["created"] == 3
    calendars = sorted(c["calendar"] for c in cal.created)
    assert calendars == ["cal-fam", "cal-shared", "primary"]
    assert set(store.rows) == {
        ("2026-09-10", "primary"),
        ("2026-09-10", "cal-fam"),
        ("2026-09-12", "cal-shared"),
    }
    assert store.rows[("2026-09-10", "primary")]["event_id"].startswith("new")
    assert store.rebuilt == 1
    # Stamped with the rebuild's start, so a webhook mid-rebuild stays dirty.
    assert store.rebuilt_at == NOW


def test_rebuild_updates_deletes_and_recreates_on_404(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] Work thing", "2026-09-10")])
    monkeypatch.setattr(
        tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "cached")
    )
    store = Store(
        rows=[
            {
                "day": "2026-09-10",
                "calendar_id": "primary",
                "event_id": "old",
                "content_hash": "stale",
                "task_gids": [],
            },
            {
                "day": "2026-09-11",
                "calendar_id": "primary",
                "event_id": "gone",
                "content_hash": "x",
                "task_gids": [],
            },
        ]
    )
    store.patch(monkeypatch)
    cal = Cal(patch_404=True)
    cal.patch(monkeypatch)

    out = h.run()
    assert out["outcome"] == "ok"
    assert out["updated"] == 0 and out["created"] == 1 and out["deleted"] == 1
    assert cal.deleted == ["gone"]
    assert store.rows[("2026-09-10", "primary")]["event_id"] == "new1"
    assert ("2026-09-11", "primary") not in store.rows


def test_rebuild_updates_changed_event_in_place(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] Work thing", "2026-09-10")])
    monkeypatch.setattr(
        tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "cached")
    )
    store = Store(
        rows=[
            {
                "day": "2026-09-10",
                "calendar_id": "primary",
                "event_id": "e9",
                "content_hash": "stale",
                "task_gids": ["1"],
            }
        ]
    )
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)

    out = h.run()
    assert out["outcome"] == "ok" and out["updated"] == 1 and out["created"] == 0
    assert cal.created == [] and cal.patched[0][0] == "e9"
    row = store.rows[("2026-09-10", "primary")]
    assert row["event_id"] == "e9" and row["content_hash"] != "stale"


def test_rebuild_adopts_existing_digest_event_instead_of_creating(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] Work thing", "2026-09-10")])
    monkeypatch.setattr(
        tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "cached")
    )
    store = Store()
    store.patch(monkeypatch)
    cal = Cal(search_hits=[{"event_id": "found", "title": "2 tasks due", "start": "2026-09-10"}])
    cal.patch(monkeypatch)

    out = h.run()
    assert out["adopted"] == 1 and out["created"] == 0
    assert cal.created == [] and cal.patched[0][0] == "found"
    assert store.rows[("2026-09-10", "primary")]["event_id"] == "found"


def test_calendar_error_is_partial_and_continues(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] A", "2026-09-10"), _task("2", "[P1] B", "2026-09-11")])
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: ([], "cached"))
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.HTTPStatusError(
                "500",
                request=httpx.Request("POST", "https://s/events"),
                response=httpx.Response(500),
            )
        return {"event_id": "ok1", "calendar_id": kw["calendar"]}

    monkeypatch.setattr(sapi, "create_event", flaky)
    out = h.run()
    assert out["outcome"] == "partial" and out["created"] == 1 and out["errors"] == 1
    assert store.rebuilt == 1


def test_db_error_during_apply_aborts_rebuild(env, monkeypatch):
    """A failed statement poisons the pg8000 transaction — stop, do not keep
    writing calendar events whose rows will all roll back."""
    _asana(monkeypatch, [_task("1", "[P1] A", "2026-09-10"), _task("2", "[P1] B", "2026-09-11")])
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: ([], "cached"))
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)

    def boom(conn, **kw):
        raise RuntimeError("db")

    monkeypatch.setattr(repo, "upsert_event", boom)
    out = h.run()
    assert out["outcome"] == "error"
    assert len(cal.created) == 1
    assert store.rebuilt == 0


def test_listing_error_aborts_without_touching_calendar(env, monkeypatch):
    def boom():
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "list_projects", boom)
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)
    out = h.run()
    assert out["outcome"] == "error" and cal.created == [] and store.rebuilt == 0

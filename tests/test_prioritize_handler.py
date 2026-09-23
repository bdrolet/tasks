import json
from datetime import date, datetime, timedelta, timezone

import pytest

import clients.asana as asana
import clients.pubsub as ps
from handlers import prioritize as h
from repo import prioritize as repo
from services import custom_fields as cf
from services import enrichment as en
from services import managed_projects

TODAY = date(2026, 9, 23)
TASK = {
    "gid": "t1",
    "name": "[P1] Reply to lawyer",
    "notes": "body",
    "html_notes": "<body>body</body>",
    "completed": False,
    "completed_at": None,
    "due_on": "2026-09-30",
    "due_at": None,
    "start_on": None,
    "created_at": "2026-09-01T00:00:00.000Z",
    "modified_at": "2026-09-20T00:00:00.000Z",
    "permalink_url": "https://app.asana.com/0/0/t1",
    "tags": [{"gid": "g", "name": "cheryl"}],
    "parent": None,
    "num_subtasks": 0,
    "memberships": [
        {"project": {"gid": "p1", "name": "Inbox"}, "section": {"gid": "s", "name": "Respond"}}
    ],
    "custom_fields": [],
    "dependencies": [{"gid": "d1"}],
    "dependents": [],
}
STORIES = [
    {
        "gid": "s1",
        "type": "comment",
        "text": "sent it",
        "created_by": {"name": "Ben"},
        "created_at": "2026-09-19T00:00:00Z",
    },
    {
        "gid": "s2",
        "type": "system",
        "text": "added to Inbox",
        "created_by": {"name": "Ben"},
        "created_at": "2026-09-01T00:00:00Z",
    },
]
GOOD = {
    "story_points_suggested": 3,
    "points_confidence": "medium",
    "waiting_on": None,
    "due_date_inferred": None,
    "due_date_inferred_confidence": "low",
    "impact": "high",
    "energy": "shallow",
    "latest_comment_signal": "none",
    "reason": "r",
}


class MemConn:
    """An in-memory stand-in for the repo: records calls, returns canned rows."""

    def __init__(self):
        self.facts, self.enrichment, self.overrides, self.stats = {}, {}, {}, {}
        self.scores, self.runs, self.estimated = [], [], set()
        self.deleted = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


@pytest.fixture
def db(monkeypatch):
    conn = MemConn()
    monkeypatch.setattr(h, "get_conn", lambda: conn)
    monkeypatch.setattr(repo, "upsert_facts", lambda c, f: c.facts.__setitem__(f.gid, f))
    monkeypatch.setattr(repo, "get_facts", lambda c, gid: c.facts.get(gid))
    monkeypatch.setattr(repo, "list_facts", lambda c: list(c.facts.values()))
    monkeypatch.setattr(repo, "delete_task", lambda c, gid: c.deleted.append(gid))
    monkeypatch.setattr(
        repo, "get_enrichment_hash", lambda c, gid: (c.enrichment.get(gid) or (None,))[0]
    )
    monkeypatch.setattr(
        repo,
        "upsert_enrichment",
        lambda c, gid, hsh, raw, model: c.enrichment.__setitem__(gid, (hsh, raw)),
    )
    monkeypatch.setattr(repo, "list_enrichment", lambda c: dict(c.enrichment))
    monkeypatch.setattr(repo, "list_overrides", lambda c: dict(c.overrides))
    monkeypatch.setattr(repo, "list_stats", lambda c: dict(c.stats))
    monkeypatch.setattr(repo, "clear_pin", lambda c, gid: None)
    monkeypatch.setattr(
        repo, "snapshot_completion", lambda c, f: c.stats.__setitem__(f.gid, "snap")
    )
    monkeypatch.setattr(repo, "replace_scores", lambda c, s: c.scores.append(s))
    monkeypatch.setattr(repo, "insert_run", lambda c, **kw: (c.runs.append(kw), len(c.runs))[1])

    def claim(c, gid, pts):
        if gid in c.estimated:
            return False
        c.estimated.add(gid)
        return True

    monkeypatch.setattr(repo, "claim_estimate", claim)
    return conn


@pytest.fixture
def asana_fake(monkeypatch):
    calls = {"detail": [], "stories": [], "subtasks": [], "points": [], "comments": []}
    monkeypatch.setattr(
        asana,
        "get_task_detail",
        lambda gid, opt_fields=None: (calls["detail"].append(gid), dict(TASK))[1],
    )
    monkeypatch.setattr(
        asana, "get_stories", lambda gid: (calls["stories"].append(gid), list(STORIES))[1]
    )
    monkeypatch.setattr(asana, "get_subtasks", lambda gid: (calls["subtasks"].append(gid), [])[1])
    monkeypatch.setattr(
        asana,
        "create_story",
        lambda gid, text=None, html_text=None: (calls["comments"].append(text), {"gid": "c"})[1],
    )
    monkeypatch.setattr(cf, "set_story_points", lambda gid, pts: calls["points"].append((gid, pts)))
    monkeypatch.setattr(cf, "read", lambda task: (None, None))
    return calls


@pytest.fixture
def model(monkeypatch):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps(GOOD)

    monkeypatch.setattr(en, "extract", lambda **kw: en.parse(fake(**kw)))
    return calls


def test_facts_from_maps_fields_and_comments():
    facts, comments = h.facts_from(TASK, STORIES)
    assert (
        facts.priority == "P1" and facts.project_name == "Inbox" and facts.dependencies == ("d1",)
    )
    assert facts.tags == ("cheryl",) and facts.due_on == date(2026, 9, 30)
    assert facts.created_at.tzinfo is not None
    assert comments == [
        {"text": "sent it", "created_by": "Ben", "created_at": "2026-09-19T00:00:00Z"}
    ]
    assert facts.content_hash == en.content_hash(TASK["name"], TASK["notes"], comments)


def test_task_changed_gathers_enriches_writes_back_and_rescores(db, asana_fake, model):
    h.handle_task_changed("t1", today=TODAY)
    assert asana_fake["detail"] == ["t1"] and asana_fake["stories"] == ["t1"]
    assert len(model) == 1
    assert db.enrichment["t1"][1]["story_points_suggested"] == 3
    assert asana_fake["points"] == [("t1", 3)]
    assert asana_fake["comments"] == ["Estimated 3 points — adjust if wrong."]
    assert (
        len(db.scores) == 1 and db.runs[0]["kind"] == "event" and db.runs[0]["trigger_gid"] == "t1"
    )


def test_second_delivery_is_cached_and_writes_back_once(db, asana_fake, model):
    h.handle_task_changed("t1", today=TODAY)
    h.handle_task_changed("t1", today=TODAY)
    assert len(model) == 1
    assert asana_fake["points"] == [("t1", 3)]
    assert len(db.scores) == 2


def test_model_failure_does_not_raise_and_is_not_cached(db, asana_fake, monkeypatch):
    def boom(**kw):
        raise RuntimeError("down")

    monkeypatch.setattr(en, "extract", boom)
    h.handle_task_changed("t1", today=TODAY)
    assert "t1" not in db.enrichment and len(db.scores) == 1
    assert db.scores[0].by_gid()["t1"].components["unenriched"] is True


def test_asana_404_deletes_rows_and_rescores(db, asana_fake, monkeypatch):
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: None)
    h.handle_task_changed("t1", today=TODAY)
    assert db.deleted == ["t1"] and len(db.scores) == 1


def test_asana_error_raises_for_redelivery(db, asana_fake, monkeypatch):
    def boom(gid, opt_fields=None):
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "get_task_detail", boom)
    with pytest.raises(RuntimeError):
        h.handle_task_changed("t1", today=TODAY)


def test_completed_task_snapshots_stats_and_skips_enrichment(db, asana_fake, model, monkeypatch):
    done = dict(TASK, completed=True, completed_at="2026-09-22T10:00:00.000Z")
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: done)
    h.handle_task_changed("t1", today=TODAY)
    assert db.stats["t1"] == "snap" and model == []


def test_subtasks_are_gathered_with_parent_project(db, asana_fake, model, monkeypatch):
    parent = dict(TASK, num_subtasks=1)
    sub = dict(
        TASK, gid="t1-sub", name="child", parent={"gid": "t1"}, memberships=[], num_subtasks=0
    )
    monkeypatch.setattr(
        asana, "get_task_detail", lambda gid, opt_fields=None: parent if gid == "t1" else sub
    )
    monkeypatch.setattr(asana, "get_subtasks", lambda gid: [{"gid": "t1-sub", "completed": False}])
    h.handle_task_changed("t1", today=TODAY)
    assert db.facts["t1"].num_open_subtasks == 1
    assert db.facts["t1-sub"].project_name == "Inbox" and db.facts["t1-sub"].parent_gid == "t1"


def test_handle_dispatches_on_kind(monkeypatch):
    seen = []
    monkeypatch.setattr(
        h, "handle_task_changed", lambda gid, today=None: seen.append(("task", gid))
    )
    monkeypatch.setattr(h, "handle_day_changed", lambda: seen.append(("day", None)))
    h.handle({"kind": "task_changed", "gid": "t9"})
    h.handle({"kind": "day_changed"})
    h.handle({"kind": "mystery"})
    assert seen == [("task", "t9"), ("day", None)]


def _facts(gid, **kw):
    f, _ = h.facts_from(dict(TASK, gid=gid, **kw), [])
    return f


def test_day_changed_first_run_has_nothing_to_defer(db, monkeypatch):
    monkeypatch.setattr(repo, "last_daily_run", lambda c: None)
    monkeypatch.setattr(h, "heal", lambda c: 0)
    bumped = []
    monkeypatch.setattr(repo, "bump_deferred", lambda c, gids, today: bumped.extend(gids))
    out = h.handle_day_changed(today=TODAY)
    assert bumped == [] and out["deferred"] == 0 and db.runs[-1]["kind"] == "daily"


def test_day_changed_bumps_unstarted_offers_and_marks_started(db, monkeypatch):
    yesterday = TODAY - timedelta(days=1)
    db.facts["a"] = _facts("a")
    db.facts["b"] = _facts(
        "b", custom_fields=[{"name": "Started at", "date_value": {"date": yesterday.isoformat()}}]
    )
    db.facts["c"] = _facts(
        "c", completed=True, completed_at=f"{yesterday.isoformat()}T18:00:00.000Z"
    )
    run = {
        "run_id": 3,
        "today": yesterday,
        "top": [{"gid": g, "rank": i, "started": None} for i, g in enumerate("abc", 1)],
    }
    monkeypatch.setattr(repo, "last_daily_run", lambda c: run)
    tops = []
    monkeypatch.setattr(repo, "set_run_top", lambda c, rid, top: tops.append((rid, top)))
    bumped = []
    monkeypatch.setattr(repo, "bump_deferred", lambda c, gids, today: bumped.extend(gids))
    monkeypatch.setattr(h, "heal", lambda c: 0)
    out = h.handle_day_changed(today=TODAY)
    assert bumped == ["a"] and out == {"deferred": 1, "started": 2, "healed": 0}
    assert [t["started"] for t in tops[0][1]] == [False, True, True]


def test_heal_republishes_newer_missing_and_stale_enrichment(db, monkeypatch):
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({"p1": {"done": None}}))
    old = datetime(2026, 9, 10, tzinfo=timezone.utc)
    listing = [
        {"gid": "fresh", "modified_at": "2026-09-01T00:00:00.000Z", "num_subtasks": 0},
        {"gid": "newer", "modified_at": "2026-09-21T00:00:00.000Z", "num_subtasks": 0},
        {"gid": "missing", "modified_at": "2026-09-01T00:00:00.000Z", "num_subtasks": 0},
        {"gid": "stale-enrich", "modified_at": "2026-09-01T00:00:00.000Z", "num_subtasks": 0},
        {"gid": "parent", "modified_at": "2026-09-01T00:00:00.000Z", "num_subtasks": 1},
    ]
    monkeypatch.setattr(
        asana, "list_project_tasks", lambda gid, only_open=False, opt_fields=None: listing
    )
    monkeypatch.setattr(
        asana,
        "get_subtasks",
        lambda gid: [
            {"gid": "child", "modified_at": "2026-09-21T00:00:00.000Z", "completed": False}
        ],
    )
    monkeypatch.setattr(
        repo,
        "list_facts_index",
        lambda c: {
            "fresh": (old, "h1"),
            "newer": (old, "h2"),
            "stale-enrich": (old, "h3"),
            "parent": (old, "h4"),
            "child": (old, "h5"),
        },
    )
    monkeypatch.setattr(
        repo,
        "list_enrichment",
        lambda c: {
            "fresh": ("h1", {}),
            "newer": ("h2", {}),
            "stale-enrich": ("OLD", {}),
            "parent": ("h4", {}),
            "child": ("h5", {}),
        },
    )
    published = []
    monkeypatch.setattr(
        ps, "publish_task_changed", lambda gid, source: published.append((gid, source))
    )
    assert h.heal(db) == 4
    assert {g for g, _ in published} == {"newer", "missing", "stale-enrich", "child"}
    assert all(s == "heal" for _, s in published)

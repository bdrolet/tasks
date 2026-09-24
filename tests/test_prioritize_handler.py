import json
from datetime import date, datetime, timedelta, timezone

import pytest

import clients.asana as asana
import clients.pubsub as ps
from handlers import prioritize as h
from models.prioritize import TaskFacts
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
        {"project": {"gid": "p1", "name": "Work"}, "section": {"gid": "s", "name": "Respond"}}
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
        self.last_offered = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _upsert_facts(c, f):
    # Mirrors the real SQL: points_estimated is excluded from the UPDATE SET,
    # so it survives across deliveries — only claim_estimate ever writes it.
    prev = c.facts.get(f.gid)
    if prev is not None and f.points_estimated is None:
        f = TaskFacts(**(f.__dict__ | {"points_estimated": prev.points_estimated}))
    c.facts[f.gid] = f


@pytest.fixture
def db(monkeypatch):
    conn = MemConn()
    monkeypatch.setattr(h, "get_conn", lambda: conn)
    monkeypatch.setattr(repo, "upsert_facts", _upsert_facts)
    monkeypatch.setattr(repo, "get_facts", lambda c, gid: c.facts.get(gid))
    monkeypatch.setattr(repo, "list_facts", lambda c: list(c.facts.values()))
    monkeypatch.setattr(repo, "delete_task", lambda c, gid: c.deleted.append(gid))
    monkeypatch.setattr(repo, "get_enrichment", lambda c, gid: c.enrichment.get(gid))
    monkeypatch.setattr(
        repo,
        "upsert_enrichment",
        lambda c, gid, hsh, raw, model: c.enrichment.__setitem__(gid, (hsh, raw)),
    )
    monkeypatch.setattr(repo, "list_enrichment", lambda c: dict(c.enrichment))
    monkeypatch.setattr(repo, "list_overrides", lambda c: dict(c.overrides))
    monkeypatch.setattr(repo, "list_stats", lambda c: dict(c.stats))

    def last_offered(c, *, today, days=30):
        # Emulates the SQL window: daily runs from [today - days, today).
        return {
            p: d for p, d in c.last_offered.items() if today - timedelta(days=days) <= d < today
        }

    monkeypatch.setattr(repo, "project_last_offered", last_offered)
    monkeypatch.setattr(repo, "clear_pin", lambda c, gid: None)
    monkeypatch.setattr(
        repo, "snapshot_completion", lambda c, f: c.stats.__setitem__(f.gid, "snap")
    )
    monkeypatch.setattr(repo, "replace_scores", lambda c, s: c.scores.append(s))
    monkeypatch.setattr(repo, "insert_run", lambda c, **kw: (c.runs.append(kw), len(c.runs))[1])
    monkeypatch.setattr(
        repo, "lock_rescore", lambda c: c.__dict__.setdefault("locks", []).append(1)
    )

    def claim(c, gid, pts):
        if gid in c.estimated:
            return False
        c.estimated.add(gid)
        return True

    monkeypatch.setattr(repo, "claim_estimate", claim)

    def set_points(c, gid, pts):
        c.__dict__.setdefault("points_set", []).append((gid, pts))
        if gid in c.facts:
            c.facts[gid] = TaskFacts(**(c.facts[gid].__dict__ | {"story_points": pts}))

    monkeypatch.setattr(repo, "set_story_points", set_points)
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
    monkeypatch.setattr(cf, "field_gid", lambda name: "cf-points")
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({"p1": {"done": None}}))
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
    assert facts.priority == "P1" and facts.project_name == "Work" and facts.dependencies == ("d1",)
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
    assert db.facts["t1-sub"].project_name == "Work" and db.facts["t1-sub"].parent_gid == "t1"


def test_gather_subtask_gid_directly_keeps_its_project(db, asana_fake, model, monkeypatch):
    """A task_changed for a subtask's own gid (e.g. republished by heal) must
    still resolve project_name from the parent — not overwrite it with None."""
    parent = dict(TASK, num_subtasks=1)
    sub = dict(
        TASK, gid="t1-sub", name="child", parent={"gid": "t1"}, memberships=[], num_subtasks=0
    )
    monkeypatch.setattr(
        asana, "get_task_detail", lambda gid, opt_fields=None: sub if gid == "t1-sub" else parent
    )
    monkeypatch.setattr(asana, "get_subtasks", lambda gid: [])
    h.handle_task_changed("t1-sub", today=TODAY)
    assert db.facts["t1-sub"].project_name == "Work"


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


# ---- D6/D7 write-back and rescore-shape coverage ---------------------------


def test_write_back_failure_keeps_the_claim_and_skips_retry(db, asana_fake, model, monkeypatch):
    calls = []

    def boom(gid, pts):
        calls.append((gid, pts))
        raise RuntimeError("asana down")

    monkeypatch.setattr(cf, "set_story_points", boom)
    h.handle_task_changed("t1", today=TODAY)
    assert "t1" in db.estimated
    assert len(calls) == 1
    assert len(model) == 1

    h.handle_task_changed("t1", today=TODAY)
    assert len(calls) == 1  # content hash cached — no retry, no exception
    assert len(model) == 1


def test_points_estimated_persists_and_feeds_scoring(db, asana_fake, model):
    seeded, _ = h.facts_from(TASK, [])
    db.facts["t1"] = TaskFacts(**(seeded.__dict__ | {"points_estimated": 5}))
    db.estimated.add("t1")  # already claimed by a prior delivery

    h.handle_task_changed("t1", today=TODAY)

    assert db.facts["t1"].points_estimated == 5
    assert asana_fake["points"] == []  # already claimed — no new Asana write
    t = db.scores[-1].by_gid()["t1"]
    assert t.components["points"] == 5
    assert t.components["points_source"] == "estimate"


def test_completed_task_snapshot_happens_once_across_deliveries(db, asana_fake, monkeypatch):
    calls = []
    monkeypatch.setattr(repo, "snapshot_completion", lambda c, f: calls.append(f.gid))
    done = dict(TASK, completed=True, completed_at="2026-09-22T10:00:00.000Z")
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: dict(done))

    h.handle_task_changed("t1", today=TODAY)
    h.handle_task_changed("t1", today=TODAY)

    assert calls == ["t1"]


def test_db_error_raises_for_redelivery(db, asana_fake, model, monkeypatch):
    def boom(c, f):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo, "upsert_facts", boom)
    with pytest.raises(RuntimeError):
        h.handle_task_changed("t1", today=TODAY)


def test_event_run_is_capped_while_daily_keeps_full_selection(db, monkeypatch):
    import dataclasses

    from services import prioritize_config as pc

    bumped = dataclasses.replace(pc.load(), default_n=12, points_per_day=12)
    monkeypatch.setattr(pc, "load", lambda path=None: bumped)

    near_due = (TODAY + timedelta(days=5)).isoformat()
    for i in range(12):
        gid = f"g{i}"
        f, _ = h.facts_from(dict(TASK, gid=gid, due_on=near_due, tags=[]), [])
        db.facts[gid] = TaskFacts(**(f.__dict__ | {"story_points": 1}))

    h.rescore(db, kind="event", trigger_gid=None, today=TODAY)
    daily_scored = h.rescore(db, kind="daily", trigger_gid=None, today=TODAY)

    ranked = [t for t in daily_scored.next() if t.rank is not None]
    assert len(ranked) > h.TOP_N_LOGGED
    assert len(db.runs[-2]["top"]) == min(len(ranked), h.TOP_N_LOGGED)
    assert len(db.runs[-1]["top"]) == len(ranked)


def test_changed_hash_with_empty_field_does_not_write_back_twice(
    db, asana_fake, model, monkeypatch
):
    h.handle_task_changed("t1", today=TODAY)
    assert asana_fake["points"] == [("t1", 3)]
    assert len(model) == 1

    changed = dict(TASK, notes="body v2")
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: dict(changed))
    h.handle_task_changed("t1", today=TODAY)

    assert len(model) == 2  # content hash moved — model runs again
    assert asana_fake["points"] == [("t1", 3)]  # already claimed — no second write


def _facts(gid, **kw):
    f, _ = h.facts_from(dict(TASK, gid=gid, **kw), [])
    return f


def test_day_changed_first_run_has_nothing_to_defer(db, monkeypatch):
    monkeypatch.setattr(repo, "last_daily_run", lambda c: None)
    monkeypatch.setattr(h, "heal", lambda: 0)
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
    monkeypatch.setattr(h, "heal", lambda: 0)
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

    def fake_get_subtasks(gid, opt_fields=None):
        assert "modified_at" in (opt_fields or "")
        return [{"gid": "child", "modified_at": "2026-09-21T00:00:00.000Z", "completed": False}]

    monkeypatch.setattr(asana, "get_subtasks", fake_get_subtasks)
    monkeypatch.setattr(
        repo,
        "list_facts_index",
        lambda c: {
            "fresh": (old, "h1"),
            "newer": (old, "h2"),
            "stale-enrich": (old, "h3"),
            "parent": (old, "h4"),
            "child": (old, "h5"),
            "vanished": (old, "h6"),
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
            "vanished": ("h6", {}),
        },
    )
    monkeypatch.setattr(
        repo,
        "list_open_gids",
        lambda c: {"fresh", "newer", "stale-enrich", "parent", "child", "vanished"},
    )
    published = []
    monkeypatch.setattr(
        ps, "publish_task_changed", lambda gid, source: published.append((gid, source))
    )
    assert h.heal() == 5
    assert {g for g, _ in published} == {"newer", "missing", "stale-enrich", "child", "vanished"}
    assert all(s == "heal" for _, s in published)


# ---- final-review fixes -----------------------------------------------------


def test_claim_commits_before_write_back_and_rescore_is_its_own_transaction(
    db, asana_fake, model, monkeypatch
):
    log = []

    class LoggingConn:
        def __enter__(self):
            log.append("begin")
            return db

        def __exit__(self, *a):
            log.append("commit")

    monkeypatch.setattr(h, "get_conn", lambda: LoggingConn())
    monkeypatch.setattr(cf, "set_story_points", lambda gid, pts: log.append("write"))
    h.handle_task_changed("t1", today=TODAY)
    # A: facts + claim; write-back; the written points persisted; B: rescore.
    assert log == ["begin", "commit", "write", "begin", "commit", "begin", "commit"]
    assert "t1" in db.estimated and db.locks == [1]


def test_missing_story_points_field_claims_nothing(db, asana_fake, model, monkeypatch):
    def missing(name):
        raise RuntimeError("custom field 'Story points' missing")

    claims = []
    monkeypatch.setattr(cf, "field_gid", missing)
    monkeypatch.setattr(repo, "claim_estimate", lambda c, gid, pts: claims.append(gid))
    h.handle_task_changed("t1", today=TODAY)
    assert claims == [] and asana_fake["points"] == [] and asana_fake["comments"] == []
    assert len(db.scores) == 1


def test_unmanaged_task_is_deleted_not_upserted(db, asana_fake, model, monkeypatch):
    elsewhere = dict(TASK, memberships=[{"project": {"gid": "p-other", "name": "Someone else's"}}])
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: dict(elsewhere))
    h.handle_task_changed("t1", today=TODAY)
    assert db.deleted == ["t1"] and db.facts == {} and model == []
    assert asana_fake["points"] == []


def test_facts_prefer_the_managed_project_of_a_multi_homed_task(monkeypatch):
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({"p1": {"done": None}}))
    monkeypatch.delenv("ASANA_PROJECT_ID", raising=False)
    multi = dict(
        TASK,
        memberships=[
            {"project": {"gid": "p-other", "name": "Other"}},
            {"project": {"gid": "p1", "name": "Work"}},
        ],
    )
    facts, _ = h.facts_from(multi, [])
    assert (facts.project_gid, facts.project_name) == ("p1", "Work")


def test_subtask_in_unmanaged_project_takes_its_managed_parents(db, asana_fake, model, monkeypatch):
    parent = dict(TASK, num_subtasks=1)
    sub = dict(
        TASK,
        gid="t1-sub",
        name="child",
        parent={"gid": "t1"},
        memberships=[{"project": {"gid": "p-other", "name": "Other"}}],
    )
    monkeypatch.setattr(
        asana, "get_task_detail", lambda gid, opt_fields=None: sub if gid == "t1-sub" else parent
    )
    h.handle_task_changed("t1-sub", today=TODAY)
    assert db.facts["t1-sub"].project_gid == "p1" and db.deleted == []


def test_in_progress_task_started_before_the_offer_is_not_deferred(db, monkeypatch):
    yesterday = TODAY - timedelta(days=1)
    db.facts["a"] = _facts(
        "a",
        custom_fields=[
            {"name": "Started at", "date_value": {"date": (TODAY - timedelta(days=9)).isoformat()}}
        ],
    )
    run = {"run_id": 4, "today": yesterday, "top": [{"gid": "a", "rank": 1, "started": None}]}
    monkeypatch.setattr(repo, "last_daily_run", lambda c: run)
    monkeypatch.setattr(repo, "set_run_top", lambda c, rid, top: None)
    bumped = []
    monkeypatch.setattr(repo, "bump_deferred", lambda c, gids, today: bumped.extend(gids))
    monkeypatch.setattr(h, "heal", lambda: 0)
    assert h.handle_day_changed(today=TODAY) == {"deferred": 0, "started": 1, "healed": 0}
    assert bumped == []


def test_missing_field_is_retried_on_the_next_cached_event(db, asana_fake, model, monkeypatch):
    def missing(name):
        raise RuntimeError("custom field 'Story points' missing")

    monkeypatch.setattr(cf, "field_gid", missing)
    h.handle_task_changed("t1", today=TODAY)
    assert asana_fake["points"] == [] and "t1" not in db.estimated

    monkeypatch.setattr(cf, "field_gid", lambda name: "cf-points")  # setup script has run
    h.handle_task_changed("t1", today=TODAY)
    assert len(model) == 1  # cached — no second model call
    assert asana_fake["points"] == [("t1", 3)] and "t1" in db.estimated


# ---- nested subtasks ----------------------------------------------------------


def _chain_fake(monkeypatch, tasks: dict, subtasks: dict | None = None):
    detail_calls: list[str] = []

    def detail(gid, opt_fields=None):
        detail_calls.append(gid)
        return dict(tasks[gid]) if gid in tasks else None

    monkeypatch.setattr(asana, "get_task_detail", detail)
    monkeypatch.setattr(asana, "get_subtasks", lambda gid: list((subtasks or {}).get(gid, [])))
    return detail_calls


def _sub(gid, parent, **kw):
    return dict(
        TASK,
        gid=gid,
        name=gid,
        parent={"gid": parent},
        memberships=[],
        **({"num_subtasks": 0} | kw),
    )


def test_grandchild_gathered_directly_resolves_project_two_hops_up(
    db, asana_fake, model, monkeypatch
):
    root = dict(TASK, num_subtasks=1)
    level1 = _sub("l1", "t1", num_subtasks=1)
    grandchild = _sub("l2", "l1")
    calls = _chain_fake(monkeypatch, {"t1": root, "l1": level1, "l2": grandchild})
    h.handle_task_changed("l2", today=TODAY)
    assert calls == ["l2", "l1", "t1"]
    assert db.facts["l2"].project_name == "Work" and db.facts["l2"].project_gid == "p1"
    assert db.deleted == []


def test_gather_top_level_descends_into_nested_subtasks(db, asana_fake, model, monkeypatch):
    root = dict(TASK, num_subtasks=1)
    level1 = _sub("l1", "t1", num_subtasks=1)
    grandchild = _sub("l2", "l1")
    _chain_fake(
        monkeypatch,
        {"t1": root, "l1": level1, "l2": grandchild},
        {"t1": [{"gid": "l1", "completed": False}], "l1": [{"gid": "l2", "completed": False}]},
    )
    h.handle_task_changed("t1", today=TODAY)
    assert set(db.facts) == {"t1", "l1", "l2"}
    assert all(f.project_name == "Work" for f in db.facts.values())
    assert db.facts["t1"].num_open_subtasks == 1 and db.facts["l1"].num_open_subtasks == 1
    assert db.facts["l2"].num_open_subtasks == 0


def test_gather_level1_subtask_passes_resolved_project_to_its_children(
    db, asana_fake, model, monkeypatch
):
    root = dict(TASK, num_subtasks=1)
    level1 = _sub("l1", "t1", num_subtasks=1)
    grandchild = _sub("l2", "l1")
    _chain_fake(
        monkeypatch,
        {"t1": root, "l1": level1, "l2": grandchild},
        {"l1": [{"gid": "l2", "completed": False}]},
    )
    h.handle_task_changed("l1", today=TODAY)
    assert db.facts["l1"].project_name == "Work" and db.facts["l2"].project_name == "Work"
    assert db.facts["l1"].num_open_subtasks == 1


def test_project_walk_and_descent_stop_at_max_depth(db, asana_fake, model, monkeypatch):
    depth = h.MAX_SUBTASK_DEPTH + 2
    # Upward: a chain deeper than the bound never reaches the managed root.
    tasks = {"t1": dict(TASK, num_subtasks=1)}
    prev = "t1"
    for i in range(1, depth + 1):
        tasks[f"s{i}"] = _sub(f"s{i}", prev, num_subtasks=1)
        prev = f"s{i}"
    calls = _chain_fake(monkeypatch, tasks)
    assert h._resolve_project(tasks[prev]) == (None, None, h.MAX_SUBTASK_DEPTH)
    assert len(calls) == h.MAX_SUBTASK_DEPTH

    # Downward: gathering the root stops after MAX_SUBTASK_DEPTH levels.
    subtasks = {"t1": [{"gid": "s1", "completed": False}]}
    for i in range(1, depth):
        subtasks[f"s{i}"] = [{"gid": f"s{i + 1}", "completed": False}]
    _chain_fake(monkeypatch, tasks, subtasks)
    gathered = h.gather("t1")
    assert gathered is not None
    assert [f.gid for f, _, _ in gathered] == ["t1"] + [
        f"s{i}" for i in range(1, h.MAX_SUBTASK_DEPTH + 1)
    ]
    assert gathered[-1][0].num_open_subtasks == 0


def test_heal_republishes_a_modified_grandchild(db, monkeypatch):
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({"p1": {"done": None}}))
    old = datetime(2026, 9, 10, tzinfo=timezone.utc)
    listing = [{"gid": "top", "modified_at": "2026-09-01T00:00:00.000Z", "num_subtasks": 1}]
    monkeypatch.setattr(
        asana, "list_project_tasks", lambda gid, only_open=False, opt_fields=None: listing
    )
    subs = {
        "top": [
            {
                "gid": "child",
                "modified_at": "2026-09-01T00:00:00.000Z",
                "num_subtasks": 1,
                "completed": False,
            }
        ],
        "child": [
            {
                "gid": "grandchild",
                "modified_at": "2026-09-21T00:00:00.000Z",
                "num_subtasks": 0,
                "completed": False,
            }
        ],
    }
    listed = []

    def fake_get_subtasks(gid, opt_fields=None):
        assert opt_fields == asana.HEAL_OPT_FIELDS
        listed.append(gid)
        return subs.get(gid, [])

    monkeypatch.setattr(asana, "get_subtasks", fake_get_subtasks)
    index = {g: (old, f"h-{g}") for g in ("top", "child", "grandchild")}
    monkeypatch.setattr(repo, "list_facts_index", lambda c: index)
    monkeypatch.setattr(
        repo, "list_enrichment", lambda c: {g: (hsh, {}) for g, (_, hsh) in index.items()}
    )
    monkeypatch.setattr(repo, "list_open_gids", lambda c: set(index))
    published = []
    monkeypatch.setattr(
        ps, "publish_task_changed", lambda gid, source: published.append((gid, source))
    )
    assert h.heal() == 1
    assert published == [("grandchild", "heal")]
    assert listed == ["top", "child"]  # one listing per parent, no detail fetches


def test_written_points_are_persisted_before_the_rescore(db, asana_fake, model):
    h.handle_task_changed("t1", today=TODAY)
    assert db.points_set == [("t1", 3)]
    t = db.scores[-1].by_gid()["t1"]
    assert t.components["points_source"] == "field"


def test_failed_write_back_persists_no_points(db, asana_fake, model, monkeypatch):
    def boom(gid, pts):
        raise RuntimeError("asana down")

    monkeypatch.setattr(cf, "set_story_points", boom)
    h.handle_task_changed("t1", today=TODAY)
    assert "points_set" not in db.__dict__


def test_gather_depth_is_measured_from_the_tree_top(db, asana_fake, model, monkeypatch):
    """A 5-deep chain: gathering from l1 or from the root stores levels up to
    MAX_SUBTASK_DEPTH only — the same absolute levels heal lists."""
    tasks = {"t1": dict(TASK, num_subtasks=1)}
    subtasks = {"t1": [{"gid": "l1", "completed": False}]}
    prev = "t1"
    for i in range(1, 6):
        tasks[f"l{i}"] = _sub(f"l{i}", prev, num_subtasks=1 if i < 5 else 0)
        if i < 5:
            subtasks[f"l{i}"] = [{"gid": f"l{i + 1}", "completed": False}]
        prev = f"l{i}"
    _chain_fake(monkeypatch, tasks, subtasks)
    levels = [f"l{i}" for i in range(1, h.MAX_SUBTASK_DEPTH + 1)]

    from_l1 = h.gather("l1")
    assert from_l1 is not None
    assert [f.gid for f, _, _ in from_l1] == levels
    assert all(f.project_name == "Work" for f, _, _ in from_l1)

    from_root = h.gather("t1")
    assert from_root is not None
    assert [f.gid for f, _, _ in from_root] == ["t1", *levels]
    assert from_root[-1][0].num_open_subtasks == 0


def test_excluded_project_skips_enrichment_and_write_back(db, asana_fake, model, monkeypatch):
    inbox = dict(
        TASK,
        memberships=[{"project": {"gid": "p1", "name": "Inbox"}, "section": None}],
    )
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: dict(inbox))
    enriched = []
    monkeypatch.setattr(
        h.otel,
        "prioritize_enrich",
        type("C", (), {"add": staticmethod(lambda n, attrs: enriched.append(attrs))})(),
    )
    h.handle_task_changed("t1", today=TODAY)
    assert model == [] and "t1" not in db.enrichment
    assert asana_fake["points"] == [] and asana_fake["comments"] == []
    assert db.facts["t1"].project_name == "Inbox"
    assert enriched == [{"result": "skipped"}]
    assert db.scores[-1].by_gid()["t1"].bucket == "excluded:project"


def test_task_moved_out_of_excluded_project_enriches(db, asana_fake, model, monkeypatch):
    inbox = dict(
        TASK,
        memberships=[{"project": {"gid": "p1", "name": "Inbox"}, "section": None}],
    )
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: dict(inbox))
    h.handle_task_changed("t1", today=TODAY)
    assert model == []
    monkeypatch.setattr(asana, "get_task_detail", lambda gid, opt_fields=None: dict(TASK))
    h.handle_task_changed("t1", today=TODAY)
    assert len(model) == 1 and asana_fake["points"] == [("t1", 3)]


def test_rescore_passes_project_last_offered_into_the_scorer(db, monkeypatch):
    db.facts["a"] = _facts("a", tags=[])
    db.last_offered = {"Work": TODAY - timedelta(days=2)}
    seen = []
    real = h.pz.score_set

    def spy(*args, **kw):
        seen.append(kw.get("project_last_offered"))
        return real(*args, **kw)

    monkeypatch.setattr(h.pz, "score_set", spy)
    scored = h.rescore(db, kind="daily", trigger_gid=None, today=TODAY)
    assert seen == [{"Work": TODAY - timedelta(days=2)}]
    assert scored.by_gid()["a"].components["days_since_project_offered"] == 2


def test_todays_daily_run_does_not_reset_the_boost_for_later_rescores(db, monkeypatch):
    """After the morning daily run, an event rescore the same day must see the
    same starvation inputs as the daily run did, so the stored rank holds."""
    db.facts["a"] = _facts("a", tags=[])
    calls = []
    real = repo.project_last_offered

    def spy(c, *, today, days=30):
        calls.append(today)
        return real(c, today=today, days=days)

    monkeypatch.setattr(repo, "project_last_offered", spy)
    db.last_offered = {"Work": TODAY - timedelta(days=3)}
    daily = h.rescore(db, kind="daily", trigger_gid=None, today=TODAY)
    db.last_offered = {"Work": TODAY}  # today's daily run picked a Work task
    event = h.rescore(db, kind="event", trigger_gid="a", today=TODAY)
    assert calls == [TODAY, TODAY]
    for scored in (daily, event):
        c = scored.by_gid()["a"].components
        assert c["days_since_project_offered"] is None or c["days_since_project_offered"] >= 1
    # only today's run exists: the project reads as never offered, not 0 days
    assert event.by_gid()["a"].components["days_since_project_offered"] is None
    assert event.by_gid()["a"].components["starvation_boost"] == 0.5

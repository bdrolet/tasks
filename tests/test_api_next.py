from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

import clients.pubsub as ps
from api.main import app
from api.routers import next as next_router
from repo import prioritize as repo

client = TestClient(app)
AUTH = {"Authorization": "Bearer x"}
NOW = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def row(
    gid,
    position,
    bucket="next",
    rank=None,
    score=1.0,
    project="Inbox",
    points=2,
    energy="shallow",
    **kw,
):
    base = dict(
        task_gid=gid,
        scored_at=NOW,
        today=date(2026, 9, 23),
        bucket=bucket,
        score=score,
        position=position,
        rank=rank,
        components={
            "points": points,
            "points_source": "field",
            "effective_due": "2026-09-30",
            "soft": False,
            "energy": energy,
            "waiting_on": None,
            "unenriched": False,
            "reason": "why",
            "days_stale": 3,
            "override": {"pinned_rank": None, "snooze_until": None, "fields": []},
        },
        overcommitted=False,
        stale=False,
        stale_reason=None,
        name=f"[P1] {gid}",
        project_name=project,
        permalink_url=f"u/{gid}",
        due_on=date(2026, 9, 30),
        story_points=points,
        started_at=None,
        pinned_rank=None,
        snooze_until=None,
        overrides={},
    )
    base.update(kw)
    return base


ROWS = [
    row("a", 1, rank=1, score=3.0),
    row("b", 2, rank=2, score=2.0, project="Family"),
    row("c", 3, score=1.0, overcommitted=True),
    row(
        "w",
        4,
        bucket="nudge",
        score=0.5,
        components={**row("w", 4)["components"], "waiting_on": "vendor"},
    ),
    row("z", 5, bucket="snoozed", score=None, snooze_until=date(2026, 9, 25)),
]


@pytest.fixture(autouse=True)
def db(monkeypatch):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(next_router, "get_conn", lambda: Conn())
    monkeypatch.setattr(repo, "list_scores", lambda c: [dict(r) for r in ROWS])
    runs = []
    monkeypatch.setattr(repo, "insert_run", lambda c, **kw: (runs.append(kw), 42)[1])
    monkeypatch.setattr(ps, "publish_task_changed", lambda gid, source: None)
    return runs


def test_ranking_default_is_next_in_position_order():
    body = client.get("/ranking", headers=AUTH).json()
    assert [t["task_gid"] for t in body["tasks"]] == ["a", "b", "c"]
    assert body["total"] == 3 and body["tasks"][0]["project"] == "Inbox"
    assert "components" not in body["tasks"][0] or body["tasks"][0]["components"] is None


def test_ranking_buckets_lists_and_paging():
    assert [
        t["task_gid"] for t in client.get("/ranking?bucket=nudge", headers=AUTH).json()["tasks"]
    ] == ["w"]
    assert [
        t["task_gid"]
        for t in client.get("/ranking?list=overcommitted", headers=AUTH).json()["tasks"]
    ] == ["c"]
    assert [
        t["task_gid"] for t in client.get("/ranking?limit=1&offset=1", headers=AUTH).json()["tasks"]
    ] == ["b"]
    assert client.get("/ranking?bucket=next&list=stale", headers=AUTH).status_code == 400


def test_ranking_explain_carries_components():
    t = client.get("/ranking?explain=true", headers=AUTH).json()["tasks"][0]
    assert t["components"]["points"] == 2 and t["reason"] == "why"


def test_next_selects_and_logs_manual_run(db):
    body = client.post("/next", headers=AUTH, json={}).json()
    assert [t["task_gid"] for t in body["next"]] == [
        "a",
        "b",
        "c",
    ]  # 2+2 < 5 so a third pick is made
    assert [t["task_gid"] for t in body["overcommitted"]] == ["c"]
    assert [t["task_gid"] for t in body["nudge"]] == ["w"]
    assert body["run_id"] == 42 and db[0]["kind"] == "manual"


def test_next_n_and_energy_reselect():
    assert len(client.post("/next", headers=AUTH, json={"n": 1}).json()["next"]) == 1
    deep = client.post("/next", headers=AUTH, json={"n": 1, "energy": "deep"}).json()["next"]
    assert deep[0]["task_gid"] == "a"  # all shallow: penalty is uniform, order holds


def test_overrides_merge_and_reject_unknown(monkeypatch):
    saved = []

    def merge(c, gid, patch):
        saved.append((gid, patch))
        from models.prioritize import Overrides

        return Overrides(fields={"impact": "high"}, pinned_rank=1, snooze_until=None)

    monkeypatch.setattr(repo, "merge_overrides", merge)
    resp = client.put(
        "/tasks/t1/overrides", headers=AUTH, json={"impact": "high", "pinned_rank": 1}
    )
    assert resp.status_code == 200 and resp.json()["pinned_rank"] == 1
    assert saved == [("t1", {"impact": "high", "pinned_rank": 1})]
    assert (
        client.put("/tasks/t1/overrides", headers=AUTH, json={"colour": "red"}).status_code == 422
    )


def test_overrides_reject_zero_story_points(monkeypatch):
    saved = []
    monkeypatch.setattr(repo, "merge_overrides", lambda c, gid, patch: saved.append(patch))
    resp = client.put("/tasks/t1/overrides", headers=AUTH, json={"story_points": 0})
    assert resp.status_code == 422 and saved == []


def test_overrides_rejects_malformed_date_and_serialises_valid_ones(monkeypatch):
    saved = []

    def merge(c, gid, patch):
        saved.append((gid, patch))
        from models.prioritize import Overrides

        return Overrides(fields={}, pinned_rank=None, snooze_until=None)

    monkeypatch.setattr(repo, "merge_overrides", merge)
    assert (
        client.put("/tasks/t1/overrides", headers=AUTH, json={"snooze_until": "banana"}).status_code
        == 422
    )
    resp = client.put(
        "/tasks/t1/overrides",
        headers=AUTH,
        json={"snooze_until": "2026-09-30", "due_date_inferred": "2026-10-01"},
    )
    assert resp.status_code == 200
    assert saved == [("t1", {"snooze_until": "2026-09-30", "due_date_inferred": "2026-10-01"})]


def test_calibrate_aggregates_per_project(monkeypatch):
    monkeypatch.setattr(
        repo,
        "calibration_rows",
        lambda c: [
            {
                "project_name": "Inbox",
                "points_at_completion": 2,
                "points_estimated": 4,
                "cycle_days": 4.0,
                "times_deferred": 1,
            },
            {
                "project_name": "Inbox",
                "points_at_completion": 3,
                "points_estimated": None,
                "cycle_days": 3.0,
                "times_deferred": 0,
            },
            {
                "project_name": "Family",
                "points_at_completion": None,
                "points_estimated": None,
                "cycle_days": None,
                "times_deferred": 5,
            },
        ],
    )
    body = client.get("/calibrate", headers=AUTH).json()
    inbox = next(p for p in body["projects"] if p["project"] == "Inbox")
    assert inbox["completed"] == 2
    assert inbox["mean_cycle_days_per_point"] == pytest.approx(1.5)  # (2.0 + 1.0) / 2
    assert inbox["mean_points_ratio"] == pytest.approx(0.5)
    assert body["overall"]["deferred_histogram"] == {"0": 1, "1": 1, "5": 1}


def _with(r, **components):
    return {**r, "components": {**r["components"], **components}}


def test_next_reselection_honours_must_do_and_starvation(monkeypatch):
    rows = [
        _with(row("hi", 1, rank=2, score=3.0), due_source="horizon", starvation_boost=0.0),
        _with(
            row("must", 2, rank=1, score=0.2, project="Family", points=4),
            due_source="hard",
            days_until_due=0,
            starvation_boost=0.0,
        ),
        _with(
            row("starved", 3, score=2.5, project="Consulting", points=1),
            due_source="horizon",
            starvation_boost=0.5,
        ),
    ]
    monkeypatch.setattr(repo, "list_scores", lambda c: [dict(r) for r in rows])
    body = client.post("/next", headers=AUTH, json={"n": 2}).json()
    # must-do first despite the lowest score; starved 2.5 * 1.5 beats hi 3.0
    assert [t["task_gid"] for t in body["next"]] == ["must", "starved"]
    one = client.post("/next", headers=AUTH, json={"n": 1}).json()["next"]
    assert [t["task_gid"] for t in one] == ["must"]

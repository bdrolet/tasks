import json
from datetime import date, datetime, timezone

from models.prioritize import ScoredSet, ScoredTask, TaskFacts
from repo import prioritize as repo
from tests.test_repo import FakeConn

TS = datetime(2026, 9, 1, tzinfo=timezone.utc)
F = TaskFacts(
    gid="t1",
    project_gid="p",
    project_name="Inbox",
    parent_gid=None,
    name="[P1] x",
    permalink_url="u",
    priority="P1",
    due_on=date(2026, 9, 30),
    due_at=None,
    start_on=None,
    started_at=None,
    story_points=None,
    points_estimated=None,
    completed=False,
    completed_at=None,
    created_at=TS,
    modified_at=TS,
    tags=("a",),
    dependencies=("d",),
    dependents=(),
    num_open_subtasks=0,
    content_hash="h",
)


class RowsConn(FakeConn):
    """FakeConn returning fetchall rows and a rowcount."""

    def __init__(self, rows=None, row=None, rowcount=0):
        super().__init__(row=row)
        self.rows, self.rowcount = rows or [], rowcount

    def execute(self, query, params=None):
        cur = super().execute(query, params)  # already appends to self.executed
        rows, rc = self.rows, self.rowcount

        class C:
            def fetchone(self_inner):
                return cur.fetchone()

            def fetchall(self_inner):
                return rows

            rowcount = rc

        return C()


def test_upsert_facts_writes_every_column():
    conn = FakeConn()
    repo.upsert_facts(conn, F)
    q, params = conn.executed[0]
    assert "INSERT INTO task_facts" in q and "ON CONFLICT (task_gid) DO UPDATE" in q
    assert params[0] == "t1" and '["a"]' in params and '["d"]' in params
    assert "points_estimated = EXCLUDED" not in q  # D6: only claim_estimate writes it


def test_lock_rescore_takes_the_advisory_xact_lock():
    conn = FakeConn()
    repo.lock_rescore(conn)
    q, params = conn.executed[0]
    assert "pg_advisory_xact_lock(%s)" in q and params == (repo.RESCORE_LOCK_KEY,)


def test_row_to_facts_roundtrip():
    row = {
        "task_gid": "t1",
        "project_gid": "p",
        "project_name": "Inbox",
        "parent_gid": None,
        "name": "[P1] x",
        "permalink_url": "u",
        "priority": "P1",
        "due_on": date(2026, 9, 30),
        "due_at": None,
        "start_on": None,
        "started_at": None,
        "story_points": None,
        "points_estimated": 3,
        "completed": False,
        "completed_at": None,
        "created_at": TS,
        "modified_at": TS,
        "tags": '["a"]',
        "dependencies": ["d"],
        "dependents": [],
        "num_open_subtasks": 0,
        "content_hash": "h",
    }
    f = repo._row_to_facts(row)
    assert f.tags == ("a",) and f.dependencies == ("d",) and f.points_estimated == 3


def test_list_open_gids_queries_uncompleted_facts():
    conn = RowsConn(rows=[{"task_gid": "a"}, {"task_gid": "b"}])
    assert repo.list_open_gids(conn) == {"a", "b"}
    q, _ = conn.executed[0]
    assert "SELECT task_gid FROM task_facts WHERE NOT completed" in q


def test_claim_estimate_is_conditional():
    assert repo.claim_estimate(RowsConn(rowcount=1), "t1", 3) is True
    conn = RowsConn(rowcount=0)
    assert repo.claim_estimate(conn, "t1", 3) is False
    assert "points_estimated IS NULL" in conn.executed[0][0]


def test_set_story_points_updates_the_field_and_fetched_at():
    conn = FakeConn()
    repo.set_story_points(conn, "t1", 5)
    q, params = conn.executed[0]
    assert "UPDATE task_facts SET story_points = %s, fetched_at = now()" in q
    assert "WHERE task_gid = %s" in q and params == (5, "t1")


def test_merge_overrides_splits_columns_and_json():
    conn = RowsConn(row={"overrides": {"impact": "high"}, "pinned_rank": 2, "snooze_until": None})
    out = repo.merge_overrides(conn, "t1", {"impact": "high", "pinned_rank": 2, "waiting_on": None})
    q, params = conn.executed[1]  # [0] is the SELECT behind get_overrides
    assert "INSERT INTO task_overrides" in q
    assert out.pinned_rank == 2 and out.fields == {"impact": "high"}


def test_merge_overrides_appends_an_audit_event():
    conn = RowsConn(row={"overrides": {}, "pinned_rank": None, "snooze_until": None})
    repo.merge_overrides(conn, "t1", {"snooze_until": "2026-10-01", "impact": None})
    q, params = conn.executed[2]
    assert "INSERT INTO task_override_events" in q
    gid, patch, result, source = params
    assert gid == "t1" and source == "api"
    assert json.loads(patch) == {"snooze_until": "2026-10-01", "impact": None}
    assert json.loads(result) == {
        "overrides": {},
        "pinned_rank": None,
        "snooze_until": "2026-10-01",
    }


def test_merge_overrides_stores_empty_waiting_on_and_none_clears():
    conn = RowsConn(row={"overrides": {}, "pinned_rank": None, "snooze_until": None})
    out = repo.merge_overrides(conn, "t1", {"waiting_on": ""})
    q, params = conn.executed[1]  # [0] is the SELECT behind get_overrides
    assert "INSERT INTO task_overrides" in q
    assert json.loads(params[1]) == {"waiting_on": ""}
    assert out.fields == {"waiting_on": ""}
    stored = {"overrides": {"waiting_on": ""}, "pinned_rank": None, "snooze_until": None}
    conn = RowsConn(row=stored)
    out = repo.merge_overrides(conn, "t1", {"waiting_on": None})
    q, params = conn.executed[1]
    assert json.loads(params[1]) == {}
    assert out.fields == {}


def test_clear_pin_appends_a_completion_event_only_when_a_pin_was_cleared():
    conn = RowsConn(
        row={"overrides": {"impact": "high"}, "pinned_rank": None, "snooze_until": None}
    )
    repo.clear_pin(conn, "t1")
    q0 = conn.executed[0][0]
    assert "UPDATE task_overrides SET pinned_rank = NULL" in q0
    assert "pinned_rank IS NOT NULL" in q0 and "RETURNING" in q0
    q, params = conn.executed[1]
    assert "INSERT INTO task_override_events" in q
    assert params[0] == "t1" and json.loads(params[1]) == {"pinned_rank": None}
    assert json.loads(params[2])["overrides"] == {"impact": "high"}
    assert params[3] == "completion"

    unpinned = FakeConn()  # UPDATE matched nothing: no pin, no event
    repo.clear_pin(unpinned, "t2")
    assert len(unpinned.executed) == 1


def test_snapshot_scores_copies_the_live_set_under_the_run():
    conn = FakeConn()
    repo.snapshot_scores(conn, 7)
    q, params = conn.executed[0]
    assert q.startswith("INSERT INTO task_scores_history")
    assert "FROM task_scores" in q and params == (7,)


def test_replace_scores_deletes_then_inserts():
    conn = FakeConn()
    scored = ScoredSet(
        today=date(2026, 9, 23),
        tasks=[
            ScoredTask(gid="a", bucket="next", score=1.5, position=1, rank=1, components={"P": 1}),
        ],
    )
    repo.replace_scores(conn, scored)
    assert conn.executed[0][0].startswith("DELETE FROM task_scores")
    assert "INSERT INTO task_scores" in conn.executed[1][0]


def test_insert_run_returns_id_and_last_daily_run_parses_top():
    conn = RowsConn(row={"run_id": 7})
    assert (
        repo.insert_run(
            conn,
            kind="daily",
            today=date(2026, 9, 23),
            trigger_gid=None,
            top=[{"gid": "a"}],
            config_hash="abc123",
        )
        == 7
    )
    q, params = conn.executed[0]
    assert "config_hash" in q and params[-1] == "abc123"
    conn2 = RowsConn(row={"run_id": 7, "today": date(2026, 9, 22), "top": '[{"gid": "a"}]'})
    assert repo.last_daily_run(conn2)["top"] == [{"gid": "a"}]
    assert repo.last_daily_run(RowsConn(row=None)) is None


def test_snapshot_completion_computes_cycle_days():
    conn = FakeConn()
    done = TaskFacts(
        **(
            F.__dict__
            | {
                "completed": True,
                "completed_at": datetime(2026, 9, 25, 12, tzinfo=timezone.utc),
                "started_at": date(2026, 9, 23),
                "story_points": 3,
                "points_estimated": 5,
            }
        )
    )
    repo.snapshot_completion(conn, done)
    q, params = conn.executed[0]
    assert "INSERT INTO task_stats" in q
    assert params[-3:] == (3, 5, 2.5)  # points_at_completion, points_estimated, cycle_days


def test_get_enrichment_returns_hash_and_parsed_raw():
    conn = RowsConn(row={"content_hash": "h", "raw": '{"impact": "high"}'})
    assert repo.get_enrichment(conn, "t1") == ("h", {"impact": "high"})
    assert repo.get_enrichment(RowsConn(row=None), "t1") is None


def test_project_last_offered_reads_daily_runs_within_the_window():
    conn = RowsConn(
        rows=[
            {"project": "Consulting", "last": date(2026, 9, 22)},
            {"project": "Ben's Board", "last": "2026-09-18"},
        ]
    )
    out = repo.project_last_offered(conn, today=date(2026, 9, 23))
    assert out == {"Consulting": date(2026, 9, 22), "Ben's Board": date(2026, 9, 18)}
    q, params = conn.executed[0]
    assert "FROM prioritize_runs r" in q
    assert "CROSS JOIN LATERAL jsonb_array_elements(r.top) AS e" in q
    assert "JOIN task_facts f ON f.task_gid = e->>'gid'" in q
    # prior days only: today's own daily run must not zero the boost of the
    # projects it just picked (every later event rescore would reshuffle them)
    assert "r.kind = 'daily' AND r.today >= %s AND r.today < %s" in q
    assert "f.project_name IS NOT NULL" in q and "GROUP BY f.project_name" in q
    assert params == (date(2026, 8, 24), date(2026, 9, 23))
    repo.project_last_offered(conn, today=date(2026, 9, 23), days=7)
    assert conn.executed[1][1] == (date(2026, 9, 16), date(2026, 9, 23))

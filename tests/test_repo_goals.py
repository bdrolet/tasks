from datetime import date

from models.strategy import Goal, GoalState, Strategy
from repo import goals as repo
from tests.test_repo import FakeConn


def test_upsert_state_writes_json():
    conn = FakeConn()
    repo.upsert_state(conn, GoalState("consulting", "outcome", date(2026, 10, 9), "h", {"a": 1}))
    q, p = conn.executed[0]
    assert "INSERT INTO goal_state" in q and "ON CONFLICT (goal_id, day)" in q
    assert p[0] == "consulting" and p[1] == date(2026, 10, 9) and '"a": 1' in p[4]


def test_set_mute_clears_with_none():
    conn = FakeConn()
    repo.set_mute(conn, "finances", None)
    q, p = conn.executed[0]
    assert "INSERT INTO goal_overrides" in q and p == ("finances", None)


def test_insert_report_returns_id():
    conn = FakeConn(row={"id": 7})
    assert repo.insert_report(conn, "consulting", 4200.0, date(2026, 10, 1)) == 7


def test_snapshot_round_trip():
    conn = FakeConn()
    strat = Strategy(goals=(Goal(id="consulting", kind="outcome", weight=1.0, horizon=date(2027, 3, 31)),),
                     last_reviewed=date(2026, 10, 1), findings=("x",), text_hash="h")
    repo.save_snapshot(conn, strat)
    q, p = conn.executed[0]
    assert "INSERT INTO strategy_snapshot" in q and p[0] == "h" and '"horizon": "2027-03-31"' in p[3]
    row = {"text_hash": "h", "last_reviewed": date(2026, 10, 1), "findings": ["x"],
           "goals": [{"id": "consulting", "kind": "outcome", "weight": 1.0, "horizon": "2027-03-31"}]}
    loaded = repo.load_snapshot(FakeConn(row=row))
    assert loaded.get("consulting").horizon == date(2027, 3, 31) and loaded.findings == ("x",)
    assert repo.load_snapshot(FakeConn(row=None)) == Strategy.EMPTY

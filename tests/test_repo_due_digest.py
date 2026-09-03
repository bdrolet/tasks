import json
from datetime import date

from repo import due_digest as repo


class RowsConn:
    """FakeConn from tests/test_repo.py returns one row; this one returns many."""

    def __init__(self, rows=None, row=None):
        self.executed = []
        self._rows = rows or []
        self._row = row

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))
        rows, row = self._rows, self._row

        class Cur:
            def fetchall(self_inner):
                return rows

            def fetchone(self_inner):
                return row

        return Cur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def test_list_events_normalizes_day_and_json():
    conn = RowsConn(
        rows=[
            {
                "day": date(2026, 9, 10),
                "calendar_id": "primary",
                "event_id": "e",
                "content_hash": "h",
                "task_gids": '["1"]',
            },
            {
                "day": "2026-09-11",
                "calendar_id": "primary",
                "event_id": "f",
                "content_hash": "h",
                "task_gids": ["2"],
            },
        ]
    )
    rows = repo.list_events(conn, since=date(2026, 9, 10))
    assert rows[0]["day"] == "2026-09-10" and rows[0]["task_gids"] == ["1"]
    assert rows[1]["day"] == "2026-09-11" and rows[1]["task_gids"] == ["2"]
    query, params = conn.executed[0]
    assert "FROM due_day_events WHERE day >= %s" in query and params == (date(2026, 9, 10),)


def test_upsert_event_is_idempotent_on_key():
    conn = RowsConn()
    repo.upsert_event(
        conn,
        day="2026-09-10",
        calendar_id="primary",
        event_id="e",
        content_hash="h",
        task_gids=["1"],
    )
    query, params = conn.executed[0]
    assert "INSERT INTO due_day_events" in query
    assert "ON CONFLICT (day, calendar_id) DO UPDATE" in query
    assert params == ("2026-09-10", "primary", "e", "h", json.dumps(["1"]))


def test_delete_and_prune():
    conn = RowsConn()
    repo.delete_event(conn, day="2026-09-10", calendar_id="primary")
    repo.prune_events(conn, before=date(2026, 6, 1))
    assert "DELETE FROM due_day_events WHERE day = %s AND calendar_id = %s" in conn.executed[0][0]
    assert "DELETE FROM due_day_events WHERE day < %s" in conn.executed[1][0]


def test_bullets_get_put():
    assert repo.get_bullets(RowsConn(row=None), "g") is None
    got = repo.get_bullets(RowsConn(row={"content_hash": "h", "bullets": '["a"]'}), "g")
    assert got == {"content_hash": "h", "bullets": ["a"]}
    conn = RowsConn()
    repo.put_bullets(conn, "g", "h", ["a", "b"])
    query, params = conn.executed[0]
    assert "INSERT INTO task_bullets" in query and "ON CONFLICT (task_gid) DO UPDATE" in query
    assert params == ("g", "h", json.dumps(["a", "b"]))


def test_state_defaults_and_marks():
    assert repo.get_state(RowsConn(row=None)) == {"dirty_at": None, "last_rebuilt_at": None}
    conn = RowsConn()
    repo.mark_dirty(conn)
    repo.mark_rebuilt(conn)
    assert "dirty_at = now()" in conn.executed[0][0]
    assert "last_rebuilt_at = now()" in conn.executed[1][0]
    assert all("INSERT INTO digest_state" in q for q, _ in conn.executed)

from repo import tasks as repo_tasks


class FakeCursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    """Stands in for clients.db connections — also a context manager, since
    callers use `with get_conn() as conn`."""

    def __init__(self, row=None):
        self.executed = []
        self._row = row

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))
        return FakeCursor(self._row)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return None


def test_insert_is_idempotent_on_message_id():
    conn = FakeConn()
    repo_tasks.insert(conn, task_gid="42", message_id="m1", category="review", importance="P1")
    query, params = conn.executed[0]
    assert "INSERT INTO tasks" in query
    assert "ON CONFLICT (message_id) DO NOTHING" in query
    assert params == ("42", "m1", "review", "P1")


def test_get_gid_by_message():
    conn = FakeConn(row={"task_gid": "42"})
    assert repo_tasks.get_gid_by_message(conn, "m1") == "42"
    assert repo_tasks.get_gid_by_message(FakeConn(row=None), "m1") is None


def test_mark_completed_and_escalated():
    conn = FakeConn()
    repo_tasks.mark_completed(conn, "42")
    repo_tasks.mark_escalated(conn, "42")
    assert "completed_at" in conn.executed[0][0]
    assert "escalated_at" in conn.executed[1][0]


def test_was_escalated():
    assert repo_tasks.was_escalated(FakeConn(row={"escalated_at": "2026-07-15"}), "42")
    assert not repo_tasks.was_escalated(FakeConn(row=None), "42")

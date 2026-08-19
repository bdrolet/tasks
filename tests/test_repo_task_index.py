from repo import task_index as repo_index


class FakeCursor:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, row=None, rows=None):
        self.executed = []
        self._row = row
        self._rows = rows

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))
        return FakeCursor(self._row, self._rows)


def test_get_state():
    conn = FakeConn(row={"content_hash": "abc", "has_embedding": True})
    assert repo_index.get_state(conn, "t1") == {"content_hash": "abc", "has_embedding": True}
    assert repo_index.get_state(FakeConn(row=None), "t1") is None


def test_upsert_serializes_vector_and_coalesces():
    conn = FakeConn()
    repo_index.upsert(
        conn,
        task_gid="t1",
        title="Pay the bill",
        notes="comcast",
        project="Inbox",
        completed=False,
        due_on="2026-08-10",
        permalink_url="https://app.asana.com/x/t1",
        content_hash="abc",
        embedding=[0.5, 0.5],
    )
    query, params = conn.executed[0]
    assert "INSERT INTO task_index" in query
    assert "ON CONFLICT (task_gid) DO UPDATE" in query
    # embed-failure upserts must not destroy an existing vector
    assert "COALESCE(EXCLUDED.embedding, task_index.embedding)" in query
    assert "%s::vector" in query
    assert params[-1] == "[0.5,0.5]"  # text form, not a float list


def test_upsert_null_embedding():
    conn = FakeConn()
    repo_index.upsert(
        conn,
        task_gid="t1",
        title="x",
        notes="",
        project=None,
        completed=False,
        due_on=None,
        permalink_url=None,
        content_hash="abc",
        embedding=None,
    )
    assert conn.executed[0][1][-1] is None


def test_set_completed():
    conn = FakeConn()
    repo_index.set_completed(conn, "t1", True)
    query, params = conn.executed[0]
    assert "UPDATE task_index SET completed" in query
    assert params == (True, "t1")


def test_delete():
    conn = FakeConn()
    repo_index.delete(conn, "t1")
    query, params = conn.executed[0]
    assert "DELETE FROM task_index WHERE task_gid = %s" in query
    assert params == ("t1",)


def test_semantic_candidates_filters_and_order():
    conn = FakeConn(rows=[{"task_gid": "t1", "score": 0.9}])
    rows = repo_index.semantic_candidates(
        conn,
        query_embedding=[1.0, 0.0],
        completed=False,
        due_before="2026-09-01",
        due_after=None,
        project="Inbox",
        limit=10,
    )
    assert rows == [{"task_gid": "t1", "score": 0.9}]
    query, params = conn.executed[0]
    assert "embedding IS NOT NULL" in query
    assert "completed = %s" in query
    assert "project = %s" in query
    assert "due_on IS NOT NULL AND due_on <= %s" in query
    assert "ORDER BY score DESC" in query
    assert params == ("[1.0,0.0]", False, "Inbox", "2026-09-01", 10)


def test_semantic_candidates_no_filters():
    conn = FakeConn(rows=[])
    repo_index.semantic_candidates(
        conn,
        query_embedding=[1.0],
        completed=None,
        due_before=None,
        due_after=None,
        project=None,
        limit=25,
    )
    query, params = conn.executed[0]
    assert "completed = %s" not in query
    assert "project = %s" not in query
    assert params == ("[1.0]", 25)


class _RowsConn:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))

        class _C:
            def __init__(s, rows):
                s._rows = rows

            def fetchall(s):
                return s._rows

        return _C(self.rows)


def test_substring_candidates_ilike_title_and_notes():
    conn = _RowsConn([{"task_gid": "t1"}])
    rows = repo_index.substring_candidates(conn, query="disney", completed=None, limit=5)
    assert rows == [{"task_gid": "t1"}]
    query, params = conn.queries[0]
    assert "title ILIKE %s OR notes ILIKE %s" in query
    assert "completed" not in query.split("WHERE", 1)[1].split("ORDER")[0]
    assert "LEFT(notes, 300)" in query
    assert params == ("%disney%", "%disney%", 5)


def test_substring_candidates_completed_filter():
    conn = _RowsConn([])
    repo_index.substring_candidates(conn, query="x", completed=True, limit=3)
    query, params = conn.queries[0]
    assert "completed = %s" in query
    assert params == ("%x%", "%x%", True, 3)


def test_get_rows_by_gids_and_empty_short_circuit():
    conn = _RowsConn([{"task_gid": "a"}])
    assert repo_index.get_rows(conn, ["a", "b"]) == [{"task_gid": "a"}]
    assert "IN (%s,%s)" in conn.queries[0][0]
    assert conn.queries[0][1] == ("a", "b")
    empty = _RowsConn([])
    assert repo_index.get_rows(empty, []) == []
    assert empty.queries == []

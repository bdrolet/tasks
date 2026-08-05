import pytest

from services import task_index


class FakeCursor:
    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class FakeConn:
    def __init__(self, state=None):
        self.executed = []
        self._state = state

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))
        if query.strip().startswith("SELECT"):
            return FakeCursor(self._state)
        return FakeCursor()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _task(gid="t1", name="Pay the bill", notes="comcast", **kw):
    return {
        "gid": gid,
        "name": name,
        "notes": notes,
        "completed": kw.get("completed", False),
        "due_on": kw.get("due_on"),
        "permalink_url": f"https://app.asana.com/x/{gid}",
        "memberships": [{"project": {"gid": "p1", "name": "Inbox"}}],
    }


def test_new_task_embeds_and_upserts():
    conn = FakeConn(state=None)
    calls = []
    embedded = task_index.index_task_dict(conn, _task(), embed_fn=lambda t: calls.append(t) or [0.1])
    assert embedded is True
    assert calls == ["Pay the bill\ncomcast"]
    insert = next(q for q, p in conn.executed if "INSERT INTO task_index" in q)
    assert insert


def test_unchanged_task_skips_embed():
    chash = task_index.content_hash("Pay the bill", "comcast")
    conn = FakeConn(state={"content_hash": chash, "has_embedding": True})
    embedded = task_index.index_task_dict(
        conn, _task(), embed_fn=lambda t: pytest.fail("should not embed")
    )
    assert embedded is False


def test_changed_content_reembeds():
    conn = FakeConn(state={"content_hash": "stale", "has_embedding": True})
    assert task_index.index_task_dict(conn, _task(), embed_fn=lambda t: [0.2]) is True


def test_missing_embedding_retries_even_when_hash_matches():
    chash = task_index.content_hash("Pay the bill", "comcast")
    conn = FakeConn(state={"content_hash": chash, "has_embedding": False})
    assert task_index.index_task_dict(conn, _task(), embed_fn=lambda t: [0.3]) is True


def test_embed_failure_stores_row_and_keeps_old_hash():
    def boom(text):
        raise RuntimeError("vertex down")

    conn = FakeConn(state={"content_hash": "oldhash", "has_embedding": True})
    embedded = task_index.index_task_dict(conn, _task(), embed_fn=boom)
    assert embedded is False
    insert_q, insert_p = next((q, p) for q, p in conn.executed if "INSERT INTO" in q)
    # old hash kept → the next pass sees a mismatch and retries the embed
    assert "oldhash" in insert_p
    assert insert_p[-1] is None  # no vector written


def test_refresh_happy_path(monkeypatch):
    conn = FakeConn(state=None)
    monkeypatch.setattr(task_index.asana, "get_task_detail", lambda gid: _task(gid=gid))
    monkeypatch.setattr(task_index, "get_conn", lambda: conn)
    monkeypatch.setattr(
        task_index.vertex, "embed", lambda text, task_type: [0.5]
    )
    task_index.refresh("t9")
    assert any("INSERT INTO task_index" in q for q, p in conn.executed)


def test_refresh_task_gone(monkeypatch):
    monkeypatch.setattr(task_index.asana, "get_task_detail", lambda gid: None)
    monkeypatch.setattr(
        task_index, "get_conn", lambda: pytest.fail("no DB call for a 404")
    )
    task_index.refresh("t9")  # no exception


def test_refresh_swallows_everything(monkeypatch):
    def boom(gid):
        raise RuntimeError("asana down")

    monkeypatch.setattr(task_index.asana, "get_task_detail", boom)
    task_index.refresh("t9")  # no exception

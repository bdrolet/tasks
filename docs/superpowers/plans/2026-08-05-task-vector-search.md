# Task Vector Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Natural-language task search — `POST /search` gains a `semantic: true` mode that ranks tasks by embedding nearest-neighbor instead of substring match.

**Architecture:** New `task_index` pgvector table in the tasks DB holds every workspace task's content + 768-dim embedding. Vertex AI `gemini-embedding-001` (IAM auth, no secrets) computes vectors. One service function (`services/task_index.py::refresh`) keeps the corpus fresh from all write paths (pipeline, API, Asana webhook); a backfill script seeds/heals it. Semantic queries rank in SQL, then hydrate top-k live from Asana.

**Tech Stack:** Python 3.13, FastAPI, pgvector on Cloud SQL Postgres 16, Vertex AI REST via httpx + google-auth, pytest.

**Spec:** `docs/superpowers/specs/2026-08-05-task-vector-search-design.md` (approved). Read it before starting.

## Global Constraints

- Layer rules (CLAUDE.md): `clients/` I/O only; `repo/` takes an open connection, never opens its own; `services/` business logic, one concern per file; handlers/routers orchestrate.
- DB and embedding work in handlers is **best-effort**: Asana is the source of truth; no indexing failure may ever crash an event or fail an API request.
- A search request never 5xxs because Vertex is down — fall back to the substring path with `semantic: false` in the response.
- Embedding vectors cross the DB wire as `'[x,y,...]'` text with an explicit `::vector` cast (works for both pg8000 prod and psycopg local drivers — do NOT pass raw float lists).
- Vertex config is env vars with defaults (`VERTEX_PROJECT=bens-project-462804`, `VERTEX_REGION=us-central1`, `VERTEX_EMBED_MODEL=gemini-embedding-001`), never secrets.
- Embedding dimension is 768 everywhere; truncated Matryoshka vectors MUST be renormalized in the client.
- Tests never touch the network or a real DB (existing convention).
- Branch: `task-vector-search` (already exists, spec committed). Commit after every task.
- Run tests with `.venv/bin/pytest tests/ -q`.

---

### Task 1: Vertex embedding client

**Files:**
- Create: `clients/vertex.py`
- Modify: `clients/otel.py` (new `vertex_duration` instrument)
- Modify: `requirements.txt` (pin google-auth)
- Modify: `docs/otel-metrics.md` (document the new instrument)
- Test: `tests/test_vertex_client.py`

**Interfaces:**
- Consumes: `clients/otel.py` instruments (`otel.vertex_duration`, `otel.errors`).
- Produces: `embed(text: str, *, task_type: str) -> list[float]` — 768-dim unit vector; `task_type` is `"RETRIEVAL_DOCUMENT"` or `"RETRIEVAL_QUERY"`; raises on any failure (callers own best-effort policy). Module constant `EMBED_DIMS = 768`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_vertex_client.py`:

```python
import math

import httpx
import pytest

import clients.vertex as vertex


class FakeResponse:
    def __init__(self, values, status=200):
        self._values = values
        self.status_code = status

    def json(self):
        return {"predictions": [{"embeddings": {"values": self._values}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setattr(vertex, "_token", lambda: "tok")


def test_embed_posts_task_type_and_dims(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse([3.0, 4.0])

    monkeypatch.setattr(httpx, "post", fake_post)
    vec = vertex.embed("hello", task_type="RETRIEVAL_QUERY")
    assert "gemini-embedding-001:predict" in captured["url"]
    assert captured["json"]["instances"][0]["task_type"] == "RETRIEVAL_QUERY"
    assert captured["json"]["parameters"]["outputDimensionality"] == vertex.EMBED_DIMS
    assert captured["headers"]["Authorization"] == "Bearer tok"
    # renormalized: [3,4] has norm 5 → [0.6, 0.8]
    assert vec == pytest.approx([0.6, 0.8])
    assert math.isclose(sum(x * x for x in vec), 1.0)


def test_embed_truncates_input(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["content"] = json["instances"][0]["content"]
        return FakeResponse([1.0])

    monkeypatch.setattr(httpx, "post", fake_post)
    vertex.embed("x" * 20_000, task_type="RETRIEVAL_DOCUMENT")
    assert len(captured["content"]) == 8000


def test_embed_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: FakeResponse([1.0], status=500))
    with pytest.raises(httpx.HTTPStatusError):
        vertex.embed("hello", task_type="RETRIEVAL_QUERY")


def test_normalize_zero_vector_unchanged():
    assert vertex._normalize([0.0, 0.0]) == [0.0, 0.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_vertex_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'clients.vertex'`

- [ ] **Step 3: Add the otel instrument**

In `clients/otel.py`, after the existing no-op instrument block (line ~32, after `api_requests`), add:

```python
vertex_duration: metrics.Histogram = metrics.NoOpMeter("noop").create_histogram("noop")
```

In `setup_telemetry`, add `vertex_duration` to the second `global` line:

```python
    global claude_tokens, api_duration, api_requests, vertex_duration
```

And after the `api_requests = meter.create_counter(...)` block:

```python
    vertex_duration = meter.create_histogram(
        "vertex.api.duration", unit="ms", description="Vertex AI embed call duration by model"
    )
```

- [ ] **Step 4: Write the client**

Create `clients/vertex.py`:

```python
"""Vertex AI embedding calls — I/O only. IAM-authenticated (ADC locally,
metadata server in GCP); no API key, no secret. Provider-generic enough to
lift into the docs repo later (its spec §15 defers exactly this)."""

import logging
import math
import os
import time

import httpx

import clients.otel as otel

logger = logging.getLogger(__name__)

VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "bens-project-462804")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "us-central1")
VERTEX_EMBED_MODEL = os.environ.get("VERTEX_EMBED_MODEL", "gemini-embedding-001")

EMBED_DIMS = 768
_MAX_CHARS = 8000  # model input limit is 2048 tokens; tasks never get near this

_credentials = None


def _token() -> str:
    global _credentials
    # Deferred import — google.auth pulls in a dependency tree the CFs
    # shouldn't pay for at cold start unless embedding is actually used.
    import google.auth
    import google.auth.transport.requests

    if _credentials is None:
        _credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials.token


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def embed(text: str, *, task_type: str) -> list[float]:
    """text → 768-dim unit vector. task_type: RETRIEVAL_DOCUMENT (indexing)
    or RETRIEVAL_QUERY (searching). Raises on any failure — callers own the
    best-effort policy."""
    url = (
        f"https://{VERTEX_REGION}-aiplatform.googleapis.com/v1"
        f"/projects/{VERTEX_PROJECT}/locations/{VERTEX_REGION}"
        f"/publishers/google/models/{VERTEX_EMBED_MODEL}:predict"
    )
    t0 = time.monotonic()
    try:
        resp = httpx.post(
            url,
            json={
                "instances": [{"content": text[:_MAX_CHARS], "task_type": task_type}],
                "parameters": {"outputDimensionality": EMBED_DIMS},
            },
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception:
        otel.errors.add(1, {"handler": "vertex_embed"})
        raise
    finally:
        otel.vertex_duration.record((time.monotonic() - t0) * 1000, {"model": VERTEX_EMBED_MODEL})
    values = resp.json()["predictions"][0]["embeddings"]["values"]
    # Matryoshka truncation to 768 dims leaves vectors non-unit-length —
    # renormalize so pgvector cosine distance behaves.
    return _normalize(values)
```

- [ ] **Step 5: Pin google-auth**

In `requirements.txt`, after the `cloud-sql-python-connector` line, add:

```
# Vertex AI embeddings — IAM token via ADC/metadata server (no API key)
google-auth>=2.30
```

Then: `.venv/bin/pip install -r requirements.txt`

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_vertex_client.py tests/test_otel.py -q`
Expected: all PASS

- [ ] **Step 7: Document the metric**

In `docs/otel-metrics.md`, add to the instrument table after the `asana.api.duration` row:

```markdown
| `vertex.api.duration` | Histogram (ms) | `model` | clients/vertex.py `embed` |
```

And to the Prometheus-names paragraph add: `vertex_api_duration_milliseconds_bucket`.

- [ ] **Step 8: Commit**

```bash
git add clients/vertex.py clients/otel.py requirements.txt docs/otel-metrics.md tests/test_vertex_client.py
git commit -m "feat: Vertex AI embedding client (gemini-embedding-001, IAM auth)"
```

---

### Task 2: Corpus schema + repo layer

**Files:**
- Modify: `repo/schema.sql`
- Create: `repo/task_index.py`
- Test: `tests/test_repo_task_index.py`

**Interfaces:**
- Consumes: an open connection (clients/db.py contract — `conn.execute(sql, params)` returning a cursor with `fetchone()`/`fetchall()`).
- Produces:
  - `get_state(conn, task_gid) -> dict | None` — `{"content_hash": str, "has_embedding": bool}`
  - `upsert(conn, *, task_gid, title, notes, project, completed, due_on, permalink_url, content_hash, embedding: list[float] | None) -> None`
  - `set_completed(conn, task_gid, completed: bool) -> None`
  - `semantic_candidates(conn, *, query_embedding, completed, due_before, due_after, project, limit) -> list[dict]` — `[{"task_gid": str, "score": float}, ...]` best-first

- [ ] **Step 1: Append schema**

At the end of `repo/schema.sql` add:

```sql
-- Semantic-search corpus: one row per workspace task (incl. manual ones).
-- embedding NULL = embed pending/failed; healed by scripts/backfill_embeddings.py.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS task_index (
    task_gid      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    project       TEXT,
    completed     BOOLEAN NOT NULL DEFAULT false,
    due_on        DATE,
    permalink_url TEXT,
    content_hash  TEXT NOT NULL,
    embedding     vector(768),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

(No HNSW index — exact scan over thousands of rows is sub-millisecond; deferred per spec §10.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_repo_task_index.py`:

```python
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_task_index.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'repo.task_index'`

- [ ] **Step 4: Write the repo module**

Create `repo/task_index.py`:

```python
"""task_index read/write — the semantic-search corpus. Takes an open
connection. Vectors cross the wire as '[x,y,...]' text with an explicit
::vector cast so both drivers (pg8000 in prod, psycopg locally) behave."""

from typing import Any


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def get_state(conn: Any, task_gid: str) -> dict | None:
    return conn.execute(
        "SELECT content_hash, (embedding IS NOT NULL) AS has_embedding"
        " FROM task_index WHERE task_gid = %s",
        (task_gid,),
    ).fetchone()


def upsert(
    conn: Any,
    *,
    task_gid: str,
    title: str,
    notes: str,
    project: str | None,
    completed: bool,
    due_on: str | None,
    permalink_url: str | None,
    content_hash: str,
    embedding: list[float] | None,
) -> None:
    """Full-row upsert. embedding=None preserves any existing vector (the
    caller failed to embed; a stale vector beats no vector)."""
    conn.execute(
        """
        INSERT INTO task_index
            (task_gid, title, notes, project, completed, due_on, permalink_url,
             content_hash, embedding, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, now())
        ON CONFLICT (task_gid) DO UPDATE SET
            title         = EXCLUDED.title,
            notes         = EXCLUDED.notes,
            project       = EXCLUDED.project,
            completed     = EXCLUDED.completed,
            due_on        = EXCLUDED.due_on,
            permalink_url = EXCLUDED.permalink_url,
            content_hash  = EXCLUDED.content_hash,
            embedding     = COALESCE(EXCLUDED.embedding, task_index.embedding),
            updated_at    = now()
        """,
        (
            task_gid,
            title,
            notes,
            project,
            completed,
            due_on,
            permalink_url,
            content_hash,
            _vec(embedding) if embedding is not None else None,
        ),
    )


def set_completed(conn: Any, task_gid: str, completed: bool) -> None:
    conn.execute(
        "UPDATE task_index SET completed = %s, updated_at = now() WHERE task_gid = %s",
        (completed, task_gid),
    )


def semantic_candidates(
    conn: Any,
    *,
    query_embedding: list[float],
    completed: bool | None,
    due_before: str | None,
    due_after: str | None,
    project: str | None,
    limit: int,
) -> list[dict]:
    """Nearest neighbors by cosine distance, best score first. Filters match
    the substring path's semantics (date bounds inclusive; date filters drop
    undated tasks)."""
    where = ["embedding IS NOT NULL"]
    params: list = [_vec(query_embedding)]
    if completed is not None:
        where.append("completed = %s")
        params.append(completed)
    if project is not None:
        where.append("project = %s")
        params.append(project)
    if due_before is not None:
        where.append("due_on IS NOT NULL AND due_on <= %s")
        params.append(due_before)
    if due_after is not None:
        where.append("due_on IS NOT NULL AND due_on >= %s")
        params.append(due_after)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT task_gid, 1 - (embedding <=> %s::vector) AS score
        FROM task_index
        WHERE {" AND ".join(where)}
        ORDER BY score DESC
        LIMIT %s
        """,
        tuple(params),
    ).fetchall()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_repo_task_index.py -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add repo/schema.sql repo/task_index.py tests/test_repo_task_index.py
git commit -m "feat: task_index corpus schema + repo layer (pgvector, cosine candidates)"
```

---

### Task 3: Index service

**Files:**
- Create: `services/task_index.py`
- Test: `tests/test_task_index_service.py`

**Interfaces:**
- Consumes: `repo.task_index` (Task 2), `clients.vertex.embed` (Task 1), `clients.asana.get_task_detail(gid) -> dict | None`, `clients.db.get_conn`, `clients.otel.errors`.
- Produces:
  - `content_hash(title: str, notes: str) -> str`
  - `index_task_dict(conn, task: dict, *, embed_fn=None) -> bool` — upserts one Asana task dict (listing or detail shape); embeds only when needed; embed failure logs and preserves retryability; returns True iff an embed call was made. Never raises on embed failure (DB errors propagate to caller).
  - `refresh(task_gid: str) -> None` — fetch from Asana + index; swallows ALL failures (the best-effort write path used by handlers/routers).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_task_index_service.py`:

```python
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
    embedded = task_index.index_task_dict(
        conn, _task(), embed_fn=lambda t: calls.append(t) or [0.1]
    )
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
    monkeypatch.setattr(task_index.vertex, "embed", lambda text, task_type: [0.5])
    task_index.refresh("t9")
    assert any("INSERT INTO task_index" in q for q, p in conn.executed)


def test_refresh_task_gone(monkeypatch):
    monkeypatch.setattr(task_index.asana, "get_task_detail", lambda gid: None)
    monkeypatch.setattr(task_index, "get_conn", lambda: pytest.fail("no DB call for a 404"))
    task_index.refresh("t9")  # no exception


def test_refresh_swallows_everything(monkeypatch):
    def boom(gid):
        raise RuntimeError("asana down")

    monkeypatch.setattr(task_index.asana, "get_task_detail", boom)
    task_index.refresh("t9")  # no exception
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_task_index_service.py -q`
Expected: FAIL — `ImportError: cannot import name 'task_index' from 'services'` (module missing)

- [ ] **Step 3: Write the service**

Create `services/task_index.py`:

```python
"""Semantic-index maintenance — one concern: keep task_index in sync with
Asana task content. `refresh` is the single best-effort write path used by
the pipeline handler, the API routers, and the webhook; `index_task_dict`
is the bulk path for the backfill script (task dicts already in hand)."""

import hashlib
import logging

import clients.asana as asana
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from repo import task_index as repo_index

logger = logging.getLogger(__name__)


def content_hash(title: str, notes: str) -> str:
    return hashlib.sha256(f"{title}\n{notes}".encode()).hexdigest()


def _project_name(task: dict) -> str | None:
    ms = task.get("memberships") or []
    if not ms:
        return None
    return (ms[0].get("project") or {}).get("name")


def index_task_dict(conn, task: dict, *, embed_fn=None) -> bool:
    """Upsert one Asana task dict (listing or detail shape). Embeds only when
    content changed or no vector is stored; returns True iff an embed call was
    made. Embed failure logs and writes the row anyway — with the OLD hash, so
    the next pass retries (repo COALESCE keeps any existing vector)."""
    if embed_fn is None:
        embed_fn = lambda text: vertex.embed(text, task_type="RETRIEVAL_DOCUMENT")  # noqa: E731
    title = task.get("name") or ""
    notes = task.get("notes") or ""
    chash = content_hash(title, notes)
    state = repo_index.get_state(conn, task["gid"])
    needs_embed = state is None or state["content_hash"] != chash or not state["has_embedding"]
    embedding = None
    if needs_embed:
        try:
            embedding = embed_fn(f"{title}\n{notes}")
        except Exception:
            logger.exception("embed failed for gid=%s — row stored without vector", task["gid"])
            if state is not None:
                chash = state["content_hash"]  # keep mismatch → retried next pass
    repo_index.upsert(
        conn,
        task_gid=task["gid"],
        title=title,
        notes=notes,
        project=_project_name(task),
        completed=bool(task.get("completed")),
        due_on=task.get("due_on"),
        permalink_url=task.get("permalink_url"),
        content_hash=chash,
        embedding=embedding,
    )
    return embedding is not None


def refresh(task_gid: str) -> None:
    """Fetch the task from Asana and re-index it. Best-effort: any failure
    (Asana, DB, Vertex) logs and returns — indexing never crashes a caller."""
    try:
        task = asana.get_task_detail(task_gid)
        if task is None:
            return
        with get_conn() as conn:
            index_task_dict(conn, task)
    except Exception:
        logger.exception("task_index refresh failed for gid=%s", task_gid)
        otel.errors.add(1, {"handler": "task_index"})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_task_index_service.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add services/task_index.py tests/test_task_index_service.py
git commit -m "feat: task_index service — hash-gated embed, best-effort refresh"
```

---

### Task 4: Write-through from pipeline, API, and completion handler

**Files:**
- Modify: `handlers/task_create.py`
- Modify: `handlers/task_complete.py`
- Modify: `api/routers/tasks.py`
- Test: `tests/test_task_create.py`, `tests/test_task_complete.py`, `tests/test_api_tasks.py` (add cases to each)

**Interfaces:**
- Consumes: `services.task_index.refresh(gid)` (Task 3), `repo.task_index.set_completed` (Task 2).
- Produces: no new interfaces — existing entry points now maintain the index.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task_create.py` (uses that file's existing `_stub_db` / `_stub_enrichment` / `_capture_create` helpers and `make_email_event` — creation stub returns gid `"42"`):

```python
def test_created_task_is_indexed(monkeypatch):
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    refreshed = []
    monkeypatch.setattr(task_create.task_index, "refresh", refreshed.append)

    task_create.handle(make_email_event())
    assert refreshed == ["42"]


def test_no_task_means_no_index_refresh(monkeypatch):
    refreshed = []
    monkeypatch.setattr(task_create.task_index, "refresh", refreshed.append)
    event = make_email_event()
    event["category"] = "ignore"  # policy gate rejects

    task_create.handle(event)
    assert refreshed == []
```

Append to `tests/test_task_complete.py` (uses its `FakeConn` import):

```python
def test_complete_updates_index(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": True})
    monkeypatch.setattr(asana, "current_section", lambda task: None)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    calls = []
    monkeypatch.setattr(
        task_complete.repo_index,
        "set_completed",
        lambda conn, gid, done: calls.append((gid, done)),
    )
    task_complete.handle("42")
    assert calls == [("42", True)]


def test_uncomplete_clears_index_flag(monkeypatch):
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: {"gid": gid, "completed": False})
    calls = []
    monkeypatch.setattr(
        task_complete.repo_index,
        "set_completed",
        lambda conn, gid, done: calls.append((gid, done)),
    )
    task_complete.handle("42")
    assert calls == [("42", False)]
```

Append to `tests/test_api_tasks.py` (uses its `client`, `AUTH`, `DETAIL`, `CreatedTask` imports):

```python
def test_create_task_refreshes_index(monkeypatch):
    from api.routers import tasks as tasks_router

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    refreshed = []
    monkeypatch.setattr(tasks_router.task_index, "refresh", refreshed.append)

    resp = client.post("/tasks", json={"name": "Bare task"}, headers=AUTH)
    assert resp.status_code == 201
    assert refreshed == ["t9"]


def test_patch_task_refreshes_index(monkeypatch):
    from api.routers import tasks as tasks_router

    monkeypatch.setattr(asana, "get_task_detail", lambda gid: dict(DETAIL))
    monkeypatch.setattr(asana, "update_task", lambda gid, fields: None)
    refreshed = []
    monkeypatch.setattr(tasks_router.task_index, "refresh", refreshed.append)

    resp = client.patch("/tasks/t1", json={"completed": True}, headers=AUTH)
    assert resp.status_code == 200
    assert refreshed == ["t1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_task_create.py tests/test_task_complete.py tests/test_api_tasks.py -q`
Expected: new tests FAIL (`AttributeError` — modules don't import `task_index`/`repo_index` yet); existing tests PASS.

- [ ] **Step 3: Wire the pipeline handler**

In `handlers/task_create.py`:

Change the services import line to include `task_index`:

```python
from services import deadline, email_summary, policy, sections, tags, task_content, task_index
```

At the end of `handle`, immediately before the final `logger.info(...)`:

```python
    # Index for semantic search — best-effort by construction (refresh
    # swallows all failures).
    task_index.refresh(task.gid)
```

- [ ] **Step 4: Wire the completion handler**

In `handlers/task_complete.py`:

Add import:

```python
from repo import task_index as repo_index
```

Replace the not-completed early-return block:

```python
    if not task.get("completed"):
        logger.info("Task %s not completed (uncomplete event?) — ignoring", task_gid)
        return
```

with:

```python
    if not task.get("completed"):
        try:
            with get_conn() as conn:
                repo_index.set_completed(conn, task_gid, False)
        except Exception:
            logger.exception("task_index uncomplete update failed for gid=%s", task_gid)
        logger.info("Task %s not completed (uncomplete event) — index flag cleared", task_gid)
        return
```

And extend the existing mark-completed block to update the index in the same connection:

```python
    try:
        with get_conn() as conn:
            repo_tasks.mark_completed(conn, task_gid)
            repo_index.set_completed(conn, task_gid, True)
    except Exception:
        logger.exception("completed_at update failed for gid=%s", task_gid)
```

- [ ] **Step 5: Wire the API routers**

In `api/routers/tasks.py`:

Add to the services imports (the file already imports `tags_service` etc. — follow its style):

```python
from services import task_index
```

In `create_task`, before the final `return CreatedTaskResponse(...)`:

```python
    task_index.refresh(created.gid)
```

In `patch_task`, before the final `return {"status": "updated", "task_gid": gid}`:

```python
    task_index.refresh(gid)
```

(Both calls sit OUTSIDE the `with translate_asana_errors():` block — refresh is self-contained best-effort and must not turn an indexing hiccup into an API error.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_task_create.py tests/test_task_complete.py tests/test_api_tasks.py -q`
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add handlers/task_create.py handlers/task_complete.py api/routers/tasks.py tests/
git commit -m "feat: index write-through from pipeline, API, and completion paths"
```

---

### Task 5: Webhook-driven freshness

**Files:**
- Modify: `handlers/asana_webhook.py`
- Test: `tests/test_asana_webhook.py` (create)

**Interfaces:**
- Consumes: `services.task_index.refresh(gid)` (Task 3), existing `handlers.task_complete.handle`.
- Produces: `receive()` now also indexes on `added` and `changed(name|notes|due_on)` task events.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_asana_webhook.py`:

```python
import hashlib
import hmac
import json

import pytest

from handlers import asana_webhook

SECRET = "whsec"


@pytest.fixture(autouse=True)
def secret_env(monkeypatch):
    monkeypatch.setenv("ASANA_WEBHOOK_SECRET", SECRET)


def _signed(events):
    body = json.dumps({"events": events}).encode()
    sig = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def _capture(monkeypatch):
    refreshed, completed = [], []
    monkeypatch.setattr(asana_webhook.task_index, "refresh", refreshed.append)
    monkeypatch.setattr(asana_webhook.task_complete, "handle", completed.append)
    return refreshed, completed


def test_added_task_event_refreshes(monkeypatch):
    refreshed, completed = _capture(monkeypatch)
    body, sig = _signed([{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)
    assert refreshed == ["t1"]
    assert completed == []


def test_changed_name_event_refreshes(monkeypatch):
    refreshed, _ = _capture(monkeypatch)
    body, sig = _signed(
        [
            {
                "action": "changed",
                "resource": {"gid": "t2", "resource_type": "task"},
                "change": {"field": "name"},
            }
        ]
    )
    asana_webhook.receive(body, sig)
    assert refreshed == ["t2"]


def test_completed_event_still_completes_not_refreshes(monkeypatch):
    refreshed, completed = _capture(monkeypatch)
    body, sig = _signed(
        [
            {
                "action": "changed",
                "resource": {"gid": "t3", "resource_type": "task"},
                "change": {"field": "completed"},
            }
        ]
    )
    asana_webhook.receive(body, sig)
    assert completed == ["t3"]
    assert refreshed == []


def test_duplicate_gids_refresh_once(monkeypatch):
    refreshed, _ = _capture(monkeypatch)
    events = [
        {
            "action": "changed",
            "resource": {"gid": "t4", "resource_type": "task"},
            "change": {"field": "name"},
        },
        {
            "action": "changed",
            "resource": {"gid": "t4", "resource_type": "task"},
            "change": {"field": "notes"},
        },
    ]
    body, sig = _signed(events)
    asana_webhook.receive(body, sig)
    assert refreshed == ["t4"]


def test_non_task_and_irrelevant_events_ignored(monkeypatch):
    refreshed, completed = _capture(monkeypatch)
    body, sig = _signed(
        [
            {"action": "added", "resource": {"gid": "s1", "resource_type": "story"}},
            {
                "action": "changed",
                "resource": {"gid": "t5", "resource_type": "task"},
                "change": {"field": "assignee"},
            },
        ]
    )
    asana_webhook.receive(body, sig)
    assert refreshed == []
    assert completed == []


def test_bad_signature_rejected(monkeypatch):
    refreshed, _ = _capture(monkeypatch)
    body, _ = _signed([])
    assert asana_webhook.receive(body, "bogus") == ("", 401)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_asana_webhook.py -q`
Expected: FAIL — `AttributeError: module 'handlers.asana_webhook' has no attribute 'task_index'`

- [ ] **Step 3: Extend the dispatch**

In `handlers/asana_webhook.py`, add the import:

```python
from services import task_index
```

Replace the event loop in `receive()` with:

```python
    payload = json.loads(body or b"{}")
    handled = 0
    refresh_gids: dict[str, None] = {}  # insertion-ordered de-dupe
    for event in payload.get("events", []):
        resource = event.get("resource") or {}
        if resource.get("resource_type") != "task":
            continue
        action = event.get("action")
        field = (event.get("change") or {}).get("field")
        if action == "changed" and field == "completed":
            task_complete.handle(resource["gid"])
            handled += 1
        elif action == "added" or (action == "changed" and field in ("name", "notes", "due_on")):
            refresh_gids[resource["gid"]] = None
    for gid in refresh_gids:
        task_index.refresh(gid)
    logger.info(
        "Webhook: %d event(s) received, %d completion(s), %d index refresh(es) — signature_valid: true",
        len(payload.get("events", [])),
        handled,
        len(refresh_gids),
    )
    return "", 200
```

(Note: the original loop did not filter on `resource_type`; completion events from Asana always carry `resource_type: "task"`, so the new guard is a no-op for them — but run the existing webhook tests in `tests/test_main.py` to confirm nothing regresses. If an existing test sends a completion event without `resource_type`, add `"resource_type": "task"` to that test's fixture payload — the real Asana payload always includes it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_asana_webhook.py tests/test_main.py -q`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add handlers/asana_webhook.py tests/test_asana_webhook.py tests/test_main.py
git commit -m "feat: webhook keeps task_index fresh on added/edited tasks"
```

---

### Task 6: Semantic search API

**Files:**
- Modify: `api/routers/search.py`
- Test: `tests/test_api_search.py` (add cases)

**Interfaces:**
- Consumes: `clients.vertex.embed` (Task 1), `repo.task_index.semantic_candidates` (Task 2), `clients.db.get_conn`, existing `asana.get_task_detail`, `email_context`, `membership`, `task_search.snippet`.
- Produces: `SearchRequest.semantic: bool = False`; `SearchResult.score: float | None`; `SearchResponse.semantic: bool = False`. Fallback rule: embed/DB failure → substring path, `semantic: false`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_search.py`:

```python
import clients.vertex as vertex_client


class _IndexConn:
    """Fake DB conn yielding fixed semantic candidates."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        rows = self._rows

        class Cur:
            def fetchall(self):
                return rows

        return Cur()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _detail(gid, name, **kw):
    return {
        "gid": gid,
        "name": name,
        "notes": kw.get("notes", ""),
        "completed": kw.get("completed", False),
        "due_on": kw.get("due_on"),
        "permalink_url": f"https://app.asana.com/x/{gid}",
        "memberships": [{"project": {"gid": "p1", "name": "Inbox"}, "section": {"name": None}}],
    }


def _semantic_setup(monkeypatch, rows, details):
    from api.routers import search as search_router

    monkeypatch.setattr(vertex_client, "embed", lambda text, task_type: [0.1, 0.2])
    monkeypatch.setattr(search_router, "get_conn", lambda: _IndexConn(rows))
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: details.get(gid))


def test_semantic_search_ranked_with_scores(monkeypatch):
    _semantic_setup(
        monkeypatch,
        rows=[{"task_gid": "t1", "score": 0.91}, {"task_gid": "t2", "score": 0.72}],
        details={
            "t1": _detail("t1", "Settle the Comcast bill"),
            "t2": _detail("t2", "File expenses"),
        },
    )
    resp = client.post(
        "/search", json={"query": "invoices I need to pay", "semantic": True}, headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["semantic"] is True
    assert [r["task_gid"] for r in body["results"]] == ["t1", "t2"]  # score order kept
    assert body["results"][0]["score"] == pytest.approx(0.91)


def test_semantic_drops_deleted_tasks(monkeypatch):
    _semantic_setup(
        monkeypatch,
        rows=[{"task_gid": "t1", "score": 0.9}, {"task_gid": "gone", "score": 0.8}],
        details={"t1": _detail("t1", "Settle the Comcast bill")},  # 'gone' → None
    )
    resp = client.post("/search", json={"query": "bills", "semantic": True}, headers=AUTH)
    assert [r["task_gid"] for r in resp.json()["results"]] == ["t1"]


def test_semantic_falls_back_when_embed_fails(monkeypatch):
    def boom(text, task_type):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(vertex_client, "embed", boom)
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    monkeypatch.setattr(
        asana, "list_project_tasks", lambda gid, **kw: [_task("t1", "Renew passport")]
    )
    monkeypatch.setattr(asana, "list_my_tasks", lambda **kw: [])
    resp = client.post("/search", json={"query": "passport", "semantic": True}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["semantic"] is False  # degraded, flagged
    assert [r["task_gid"] for r in body["results"]] == ["t1"]


def test_semantic_requires_query():
    resp = client.post("/search", json={"query": "  ", "semantic": True}, headers=AUTH)
    assert resp.status_code == 400


def test_semantic_unknown_project_400(monkeypatch):
    monkeypatch.setattr(vertex_client, "embed", lambda text, task_type: [0.1])
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    resp = client.post(
        "/search", json={"query": "x", "semantic": True, "project": "Nope"}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "known_projects" in resp.json()["detail"]


def test_substring_path_unchanged_response_shape(monkeypatch):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Inbox"}])
    monkeypatch.setattr(
        asana, "list_project_tasks", lambda gid, **kw: [_task("t1", "Renew passport")]
    )
    monkeypatch.setattr(asana, "list_my_tasks", lambda **kw: [])
    body = client.post("/search", json={"query": "passport"}, headers=AUTH).json()
    assert body["semantic"] is False
    assert body["results"][0]["score"] is None
```

(`pytest` import already exists in the file; keep the existing `_task` helper.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api_search.py -q`
Expected: new tests FAIL (`semantic` field unknown / missing keys); existing tests PASS.

- [ ] **Step 3: Implement**

In `api/routers/search.py`:

Add imports at the top:

```python
import clients.vertex as vertex
from clients.db import get_conn
from repo import task_index as repo_index
```

Extend the models:

```python
class SearchRequest(BaseModel):
    query: str = ""
    project: str | None = None
    completed: bool | None = False
    due_before: str | None = None
    due_after: str | None = None
    limit: int = Field(default=25, ge=1, le=100)
    semantic: bool = False  # natural-language nearest-neighbor ranking
```

`SearchResult` gains:

```python
    score: float | None = None  # cosine similarity 0..1; semantic hits only
```

`SearchResponse` becomes:

```python
class SearchResponse(BaseModel):
    results: list[SearchResult]
    semantic: bool = False  # true iff the semantic path served this response
```

Extract the result construction (currently the loop at the end of `search()`) into a helper so both paths share it:

```python
def _to_result(
    t: dict, *, query: str, email: dict, parent_name: str | None, score: float | None = None
) -> SearchResult:
    project_name, section_name = membership(t)
    return SearchResult(
        task_gid=t["gid"],
        name=t.get("name") or "",
        project=project_name,
        section=section_name,
        due_on=t.get("due_on"),
        completed=bool(t.get("completed")),
        permalink_url=t.get("permalink_url"),
        snippet=task_search.snippet(t.get("notes"), query),
        message_id=email.get("message_id"),
        category=email.get("category"),
        importance=email.get("importance"),
        parent=parent_name or (t.get("parent") or {}).get("name"),
        score=score,
    )
```

Rewrite the existing `search()` loop to use `_to_result(t, query=body.query, email=ctx.get(t["gid"], {}), parent_name=parent_names.get(t["gid"]))` and return `SearchResponse(results=results, semantic=False)`.

Add the semantic path — at the top of `search()`:

```python
    if body.semantic:
        if not body.query.strip():
            raise HTTPException(status_code=400, detail="semantic search requires a query")
        semantic_response = _semantic_search(body)
        if semantic_response is not None:
            return semantic_response
        logger.warning("semantic search unavailable — serving substring results")
```

And the function:

```python
def _semantic_search(body: SearchRequest) -> SearchResponse | None:
    """None = semantic ranking unavailable (embed or DB failure) — the caller
    falls back to the substring path. Asana errors and bad requests raise."""
    project_name = None
    if body.project:
        with translate_asana_errors():
            projects = asana.list_projects()
        project = task_search.resolve_project(projects, body.project)
        if project is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"unknown project: {body.project}",
                    "known_projects": [p["name"] for p in projects],
                },
            )
        project_name = project["name"]

    try:
        qvec = vertex.embed(body.query, task_type="RETRIEVAL_QUERY")
        with get_conn() as conn:
            rows = repo_index.semantic_candidates(
                conn,
                query_embedding=qvec,
                completed=body.completed,
                due_before=body.due_before,
                due_after=body.due_after,
                project=project_name,
                limit=body.limit,
            )
    except Exception:
        logger.exception("semantic ranking failed")
        return None

    scores = {r["task_gid"]: r["score"] for r in rows}  # insertion order = rank
    with translate_asana_errors():
        with ThreadPoolExecutor(max_workers=8) as pool:
            details = list(pool.map(asana.get_task_detail, list(scores)))
    tasks = [t for t in details if t is not None]  # deleted since indexing → drop
    ctx = email_context([t["gid"] for t in tasks])
    results = [
        _to_result(
            t,
            query=body.query,
            email=ctx.get(t["gid"], {}),
            parent_name=None,
            score=round(scores[t["gid"]], 4),
        )
        for t in tasks
    ]
    return SearchResponse(results=results, semantic=True)
```

(Semantic results keep score order — best match first — NOT the substring path's due-date sort. `snippet` is usually null for semantic hits since there may be no literal substring match; that's expected.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_search.py -q`
Expected: all PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add api/routers/search.py tests/test_api_search.py
git commit -m "feat: semantic search mode on POST /search with substring fallback"
```

---

### Task 7: Backfill script

**Files:**
- Create: `scripts/backfill_embeddings.py`

**Interfaces:**
- Consumes: `clients.asana` listing helpers, `clients.db.get_conn`, `services.task_index.index_task_dict` / `content_hash`, `repo.task_index.get_state`.
- Produces: an idempotent CLI — `--dry-run` reports what would embed; real run seeds/heals the corpus.

(Repo convention: `scripts/` are operational tools without unit tests — `test-task-create.py`, `migrate_db.py` etc. are untested. Verification is the `--dry-run` smoke run in Task 8's rollout.)

- [ ] **Step 1: Write the script**

Create `scripts/backfill_embeddings.py`:

```python
#!/usr/bin/env python3
"""Seed or heal the task_index semantic-search corpus from Asana.

Idempotent: unchanged content never re-embeds (hash-gated); rows with a
missing vector are retried. Run after deploy, or anytime to heal drift:

  scripts/fetch-env.sh   # once, for .env
  .venv/bin/python scripts/backfill_embeddings.py --dry-run
  .venv/bin/python scripts/backfill_embeddings.py

Needs .env (Asana + DB) and gcloud ADC for Vertex
(`gcloud auth application-default login`).
"""

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.asana as asana
from clients.db import get_conn
from repo import task_index as repo_index
from services import task_index


def all_tasks() -> list[dict]:
    """Every task in the workspace, incl. completed and subtasks — the same
    sweep the substring search path does."""
    projects = asana.list_projects()
    with ThreadPoolExecutor(max_workers=8) as pool:
        per_project = list(
            pool.map(lambda p: asana.list_project_tasks(p["gid"], only_open=False), projects)
        )
    tasks = [t for batch in per_project for t in batch]
    tasks += asana.list_my_tasks(only_open=False)
    with_subs = [t for t in tasks if t.get("num_subtasks")]
    if with_subs:
        with ThreadPoolExecutor(max_workers=8) as pool:
            batches = list(pool.map(lambda t: asana.get_subtasks(t["gid"]), with_subs))
        for batch in batches:
            tasks.extend(batch)
    seen: set[str] = set()
    unique = []
    for t in tasks:
        if t["gid"] not in seen:
            seen.add(t["gid"])
            unique.append(t)
    return unique


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report, don't write or embed")
    args = parser.parse_args()

    tasks = all_tasks()
    print(f"{len(tasks)} tasks listed from Asana")

    embedded = skipped = failed = 0
    with get_conn() as conn:
        for i, t in enumerate(tasks, 1):
            title, notes = t.get("name") or "", t.get("notes") or ""
            if args.dry_run:
                state = repo_index.get_state(conn, t["gid"])
                chash = task_index.content_hash(title, notes)
                if state is None or state["content_hash"] != chash or not state["has_embedding"]:
                    embedded += 1
                else:
                    skipped += 1
                continue
            if task_index.index_task_dict(conn, t):
                embedded += 1
            else:
                # skipped (hash match) or embed failure — index_task_dict logged it
                skipped += 1
            if i % 50 == 0:
                conn.commit()
                print(f"  {i}/{len(tasks)} …")
        if not args.dry_run:
            conn.commit()

    verb = "would embed" if args.dry_run else "embedded"
    print(f"done: {verb} {embedded}, skipped {skipped}, failed {failed}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"backfill failed: {e}", file=sys.stderr)
        sys.exit(1)
```

- [ ] **Step 2: Syntax check + full suite**

Run: `.venv/bin/python -m py_compile scripts/backfill_embeddings.py && .venv/bin/pytest tests/ -q`
Expected: compile OK, all tests PASS

- [ ] **Step 3: Commit**

```bash
git add scripts/backfill_embeddings.py
git commit -m "feat: idempotent task_index backfill/heal script"
```

---

### Task 8: Terraform, docs, skill, PR + rollout

**Files:**
- Create: `terraform/vertex.tf`
- Modify: `CLAUDE.md`
- Modify: `.claude/skills/searching-tasks/SKILL.md`
- Modify: `docs/superpowers/specs/2026-08-05-task-vector-search-design.md` (status line only)

**Interfaces:**
- Consumes: existing service accounts `google_service_account.tasks_events_cf`, `.tasks_webhook_cf`, `.tasks_api` (terraform/iam.tf, api.tf).
- Produces: Vertex API enabled + `roles/aiplatform.user` for all three runtime identities; updated consumer docs.

- [ ] **Step 1: Terraform**

Create `terraform/vertex.tf`:

```hcl
# Vertex AI embeddings for semantic task search (clients/vertex.py).
# Spec deviation note: main.tf says project APIs are inbox-owned, but
# aiplatform is used only by tasks — owned here, never disabled on destroy.
resource "google_project_service" "aiplatform" {
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_member" "events_cf_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

resource "google_project_iam_member" "webhook_cf_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

resource "google_project_iam_member" "tasks_api_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.tasks_api.email}"
}
```

Run: `terraform -chdir=terraform fmt && terraform -chdir=terraform validate`
Expected: `Success! The configuration is valid.`

- [ ] **Step 2: Update CLAUDE.md**

In the Stack table's **Database** row, change `tables `tasks`, `asana_tag_cache`` to `tables `tasks`, `asana_tag_cache`, `task_index` (pgvector semantic-search corpus)`.

After the **Enrichment** row, add:

```markdown
| **Embeddings** | Vertex AI `gemini-embedding-001` via `clients/vertex.py` (IAM auth — no key/secret; `VERTEX_*` env vars with in-code defaults). Corpus in `task_index`, maintained by `services/task_index.py::refresh` from pipeline/API/webhook write paths; seed/heal with `scripts/backfill_embeddings.py`. `POST /search` `semantic: true` ranks by cosine nearest-neighbor, falls back to substring on Vertex outage |
```

- [ ] **Step 3: Update the searching-tasks skill**

In `.claude/skills/searching-tasks/SKILL.md`:

Add to the Parameters table:

```markdown
| `semantic` | `false` | Natural-language nearest-neighbor ranking. Use for conceptual queries ("invoices I need to pay"); keep `false` for exact keywords. |
```

After the Parameters section, add:

```markdown
## Semantic search

Set `"semantic": true` when the query is natural language rather than a
keyword the task literally contains. Results come back best-match-first
with a `score` (cosine similarity, 0–1); `snippet` is usually null (no
literal substring). The response's top-level `semantic` flag is `false`
when the service degraded to substring ranking (embedding backend down) —
mention that if results look off. Requires a non-empty `query`.
```

And in "Presenting results", add: `- Semantic hits: order is relevance, not due date — present in given order; surface `score` only if the user asks why something matched.`

- [ ] **Step 4: Mark spec status + full suite + commit**

Change the spec's `**Status:**` line to `Approved design — implemented (see docs/superpowers/plans/2026-08-05-task-vector-search.md)`.

Run: `.venv/bin/pytest tests/ -q` — all PASS.

```bash
git add terraform/vertex.tf CLAUDE.md .claude/skills/searching-tasks/SKILL.md docs/superpowers/specs/2026-08-05-task-vector-search-design.md
git commit -m "feat: vertex terraform, docs, searching-tasks semantic mode"
```

- [ ] **Step 5: Open the PR**

Use the `/pr-open` skill (repo workflow: PRs, never direct to main — merge auto-deploys).

- [ ] **Step 6: Pre-merge infrastructure (safe before deploy)**

1. **Migrate DB** (additive only — existing tables untouched):
   ```bash
   scripts/fetch-env.sh   # if .env is stale
   (set -a; source .env; set +a; .venv/bin/python scripts/migrate_db.py)
   ```
   Expected: `Migration complete`
2. **Terraform**: run the `/terraform-plan` skill; expect exactly 4 adds (1 service + 3 IAM members). Then `/terraform-apply`.

- [ ] **Step 7: Verify locally before merge**

Use the `/verifying-pr-locally` skill, plus a manual semantic smoke against local uvicorn:

```bash
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080) &
TOKEN=$(grep 'tasks_api_token' terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
.venv/bin/python scripts/backfill_embeddings.py --dry-run   # sanity: lists tasks, counts pending
.venv/bin/python scripts/backfill_embeddings.py             # seed the corpus (real embeds, ~$0.01)
curl -s -X POST localhost:8080/search -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "things I need to pay for", "semantic": true, "limit": 5}' | python3 -m json.tool
```

Expected: `"semantic": true` with plausibly-ranked results carrying `score` values.

- [ ] **Step 8: Merge + post-deploy**

1. Merge the PR (auto-deploy watches main; CFs via `deploy.yml`, API via `deploy-api.yml`).
2. Smoke the live API:
   ```bash
   curl -s -X POST https://tasks-api.drolet.cloud/search -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"query": "things I need to pay for", "semantic": true, "limit": 5}' | python3 -m json.tool
   ```
3. Confirm webhook freshness: edit any task title in Asana, wait ~5s, re-run the query — the edit should be reflected (check `fetch-tasks-logs` for `index refresh(es)` on tasks-webhook if not).
4. Check `vertex_api_duration_milliseconds_bucket` appears in Grafana (`/querying-grafana-metrics` skill).

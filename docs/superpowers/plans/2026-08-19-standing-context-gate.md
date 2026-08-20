# Standing-Context Gate (Triage Agent) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second gate to `handlers/task_create.py` — a Sonnet 5 triage agent with read-only email/task search tools — that suppresses tasks which are moot given declared facts about Ben, prior mail, or an existing Asana task, and records every suppression.

**Architecture:** `services/triage.decide(event)` builds a prompt (today's date + the `Roles` section of `context/standing-context.md` + the email), runs the Anthropic SDK tool runner via a new `clients/claude.run_agent`, exposes four `@beta_tool` functions that delegate to existing clients (`inbox_api`, `vertex`, `repo/task_index`, `repo/tasks`, `asana`), and parses a strict-JSON `Decision`. The handler calls it after `policy.warrants_task` and before enrichment; a non-actionable decision writes a `suppressed_emails` row (and optionally a comment on a related task) instead of creating a task. A deterministic no-action-phrase veto on the Haiku key points is a backstop. `services/deadline.py` gains the `Calendar` section.

**Tech Stack:** Python 3.13, `anthropic` SDK ≥0.116 (`client.beta.messages.tool_runner`, `@beta_tool`), httpx, pgvector/Postgres via `clients/db.get_conn`, OpenTelemetry, pytest with `monkeypatch` (no network in tests).

**Spec:** `docs/superpowers/specs/2026-08-18-standing-context-gate-design.md`

## Global Constraints

- Agent model is exactly `claude-sonnet-5`, adaptive thinking, `effort: "medium"`. Haiku summary and Sonnet deadline calls are unchanged.
- Agent bounds: `max_iterations=6`, per-tool HTTP timeout 10s, whole `decide()` wall-clock deadline 60s. Every failure path creates the task (fail-open). `category == "urgent"` never invokes the agent.
- Tools are read-only. The only write the gate can cause is `asana.create_story` on a `related_task_gid`, done by the handler.
- Layer rules (CLAUDE.md): `clients/` I/O only; `repo/` takes an open connection; `services/` no direct HTTP; `handlers/` orchestrate only; `models/` pure types.
- DB writes in the handler are best-effort: failures log at WARNING/exception and never reverse a decision or crash the event.
- Metric names are prefixed `asana.` (export as `asana_`). No free-text metric attributes.
- Never call the tasks-api or inbox-api `/search` for tasks from inside the CF; task search runs in-process. Email search goes over HTTP to inbox-api (this repo never talks to Graph).
- Run tests with `.venv/bin/pytest tests/ -q` from the repo root. Lint: `.venv/bin/ruff check .` if ruff is installed (pyproject has its config); otherwise skip.
- Commit after every task. Work on branch `standing-context-gate` (already checked out). Do not touch `.claude/agents/task-lister.md` (pre-existing unrelated change).

---

### Task 1: Standing-context file, loader, and deploy allowlist

**Files:**
- Create: `context/standing-context.md`
- Create: `services/standing_context.py`
- Modify: `.github/workflows/deploy.yml` (the `paths:` list, lines 7-14)
- Test: `tests/test_standing_context.py`

**Interfaces:**
- Produces: `services.standing_context.section(name: str) -> str` — body of the `## {name}` section (case-insensitive heading match), `""` if the file or section is missing. `services.standing_context.reset_cache() -> None` for tests. Module constant `DEFAULT_PATH` and module variable `PATH` (a `pathlib.Path`, overridable via env `STANDING_CONTEXT_PATH`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_standing_context.py
from pathlib import Path

import pytest

from services import standing_context

DOC = """# Standing context

## Roles

### Assistant Coach
Ended 2026-08-14.

- admin mail is NOT actionable

## Calendar

- SFUSD fall term: 2026-08-17 to 2026-12-18.
"""


@pytest.fixture
def ctx_file(tmp_path, monkeypatch):
    path = tmp_path / "standing-context.md"
    path.write_text(DOC)
    monkeypatch.setattr(standing_context, "PATH", path)
    standing_context.reset_cache()
    yield path
    standing_context.reset_cache()


def test_section_returns_body_by_heading(ctx_file):
    roles = standing_context.section("Roles")
    assert roles.startswith("### Assistant Coach")
    assert "admin mail is NOT actionable" in roles
    assert "SFUSD" not in roles


def test_section_heading_match_is_case_insensitive(ctx_file):
    assert "SFUSD" in standing_context.section("calendar")


def test_missing_section_is_empty(ctx_file):
    assert standing_context.section("Nope") == ""


def test_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(standing_context, "PATH", tmp_path / "absent.md")
    standing_context.reset_cache()
    assert standing_context.section("Roles") == ""


def test_file_is_read_once_until_reset(ctx_file):
    assert "Assistant Coach" in standing_context.section("Roles")
    ctx_file.write_text("# Standing context\n\n## Roles\n\nChanged.\n")
    assert "Assistant Coach" in standing_context.section("Roles")  # cached
    standing_context.reset_cache()
    assert standing_context.section("Roles") == "Changed."


def test_shipped_file_has_roles_and_calendar():
    """The real context/standing-context.md must parse with both sections."""
    standing_context.reset_cache()
    real = Path(__file__).resolve().parent.parent / "context" / "standing-context.md"
    text = real.read_text()
    assert standing_context.section("Roles", text=text)
    assert standing_context.section("Calendar", text=text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_standing_context.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.standing_context'`

- [ ] **Step 3: Create the context file**

```markdown
<!-- context/standing-context.md -->
# Standing context

Declared facts about Ben that the pipeline cannot derive from mail. Read by
`services/standing_context.py`; sections are addressed by `## ` heading.
`Roles` feeds the triage agent (services/triage.py); `Calendar` feeds
deadline extraction (services/deadline.py). A fact that applies only for a
period states that period in its own prose — the model is given today's date
and decides whether it still applies. Retire a fact by deleting its block.

## Roles

### Assistant Coach, West Portal Proud Panthers (SF Microsoccer / SF Vikings)
**Ended 2026-08-14, for the 2026 fall season — that season runs 2026-09-12 to
2026-12-18.** Ben resigned; Christy Dillon is handling the replacement.
Elijah remains a player on the team.

- Coach- and admin-directed mail — schedules to review, coach admin
  requirements, Micro Admin broadcasts — is NOT actionable for this season.
- Mail about Elijah as a player — invitations, rosters, parent logistics —
  IS actionable.

## Calendar

- SFUSD 2026-27 fall term: 2026-08-17 to 2026-12-18.
- West Portal Elementary day: 7:50am-2:05pm Mon/Tue/Thu/Fri, 7:50am-12:50pm Wed.
```

- [ ] **Step 4: Write the loader**

```python
# services/standing_context.py
"""Declared facts about Ben — context/standing-context.md, sectioned by
'## ' headings. Consumers read only the section they need. Any read failure
yields "" so a missing/unreadable file degrades to 'no facts', never an error."""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "context" / "standing-context.md"
PATH = Path(os.environ.get("STANDING_CONTEXT_PATH", str(DEFAULT_PATH)))

_cache: str | None = None
_H2 = re.compile(r"^## ", re.MULTILINE)


def reset_cache() -> None:
    global _cache
    _cache = None


def _text() -> str:
    global _cache
    if _cache is None:
        try:
            _cache = PATH.read_text(encoding="utf-8")
        except Exception:
            logger.warning("standing context unreadable at %s — treating as empty", PATH)
            _cache = ""
    return _cache


def section(name: str, *, text: str | None = None) -> str:
    """Body of the `## {name}` section (heading match is case-insensitive),
    stripped; "" if absent. `text` overrides the file (tests, previews)."""
    source = _text() if text is None else text
    for chunk in _H2.split(source)[1:]:
        heading, _, body = chunk.partition("\n")
        if heading.strip().casefold() == name.casefold():
            return body.strip()
    return ""
```

- [ ] **Step 5: Add `context/**` to the deploy allowlist**

In `.github/workflows/deploy.yml`, inside `on.push.paths`, add a line after `'handlers/**'`:

```yaml
      - 'context/**'
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_standing_context.py -q`
Expected: 6 passed

- [ ] **Step 7: Commit**

```bash
git add context/standing-context.md services/standing_context.py tests/test_standing_context.py .github/workflows/deploy.yml
git commit -m "feat: standing-context file and sectioned loader"
```

---

### Task 2: inbox-api search client

**Files:**
- Modify: `clients/inbox_api.py`
- Test: `tests/test_inbox_api.py` (append)

**Interfaces:**
- Produces: `clients.inbox_api.search(query: str, *, mode: str = "graph", limit: int = 10, mailboxes: list[str] | None = None) -> list[dict]` — `POST {INBOX_API_URL}/search`, returns the `results` list (each dict has `message_id, subject, sender, sender_display, received_at, preview, mailbox, web_link, category, importance`). Raises `httpx.HTTPStatusError` on non-2xx. Module constant `SEARCH_TIMEOUT = 10`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_inbox_api.py`)

```python
def _capture_post(monkeypatch, status=200, payload=None):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", url))

    monkeypatch.setattr(inbox_api.httpx, "post", fake_post)
    return calls


def test_search_posts_query_and_returns_results(monkeypatch):
    calls = _capture_post(monkeypatch, payload={"results": [{"subject": "Bill"}]})
    rows = inbox_api.search("from:xfinity", limit=5)
    assert rows == [{"subject": "Bill"}]
    assert calls[0]["url"] == "https://inbox-api.example/search"
    assert calls[0]["json"] == {"query": "from:xfinity", "mode": "graph", "limit": 5}
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["timeout"] == inbox_api.SEARCH_TIMEOUT


def test_search_passes_mode_and_mailboxes(monkeypatch):
    calls = _capture_post(monkeypatch, payload={"results": []})
    inbox_api.search("disney", mode="db", mailboxes=["me"])
    assert calls[0]["json"]["mode"] == "db"
    assert calls[0]["json"]["mailboxes"] == ["me"]


def test_search_http_error_raises(monkeypatch):
    _capture_post(monkeypatch, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        inbox_api.search("x")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_inbox_api.py -q`
Expected: 3 FAIL — `AttributeError: module 'clients.inbox_api' has no attribute 'search'`

- [ ] **Step 3: Implement `search`** (append to `clients/inbox_api.py`; also update the module docstring's last sentence to "First pipeline consumer: the triage agent's `search_emails` / `get_email` tools (services/triage.py).")

```python
SEARCH_TIMEOUT = 10


def search(
    query: str, *, mode: str = "graph", limit: int = 10, mailboxes: list[str] | None = None
) -> list[dict]:
    """POST /search — `graph` mode is live Outlook KQL (`from:`, `subject:`,
    keywords) across the primary + shared mailboxes; `db` mode is processed
    mail with category/importance. Returns the `results` list."""
    payload: dict = {"query": query, "mode": mode, "limit": limit}
    if mailboxes:
        payload["mailboxes"] = mailboxes
    resp = httpx.post(
        f"{INBOX_API_URL}/search",
        json=payload,
        headers={"Authorization": f"Bearer {INBOX_API_TOKEN}"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_inbox_api.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add clients/inbox_api.py tests/test_inbox_api.py
git commit -m "feat: inbox_api.search for the triage agent"
```

---

### Task 3: Repo layer — task_index lookups, suppressions table

**Files:**
- Modify: `repo/task_index.py` (append)
- Create: `repo/suppressions.py`
- Modify: `repo/schema.sql` (append)
- Test: `tests/test_repo_task_index.py` (append), `tests/test_repo_suppressions.py` (new)

**Interfaces:**
- Produces: `repo.task_index.substring_candidates(conn, *, query: str, completed: bool | None, limit: int) -> list[dict]` and `repo.task_index.get_rows(conn, task_gids: list[str]) -> list[dict]`. Both return rows with keys `task_gid, title, notes, project, completed, due_on, permalink_url` where `notes` is `LEFT(notes, 300)`.
- Produces: `repo.suppressions.insert(conn, *, message_id, category, importance, subject, sender, reason, source, related_task_gid, evidence: list) -> None`, idempotent on `message_id`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_repo_task_index.py` (it already imports `repo.task_index as repo_index` — check the top of the file and reuse its fake-conn helper if one exists; otherwise use the one below):

```python
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
```

New `tests/test_repo_suppressions.py`:

```python
import json

from repo import suppressions
from tests.test_repo import FakeConn


def test_insert_writes_all_columns_and_is_idempotent():
    conn = FakeConn()
    suppressions.insert(
        conn,
        message_id="m1",
        category="review",
        importance="P1",
        subject="Your bill",
        sender="billing@xfinity.com",
        reason="autopay already processed",
        source="agent",
        related_task_gid=None,
        evidence=[{"kind": "email", "ref": "m0", "note": "Thanks for your payment"}],
    )
    query, params = conn.executed[0]
    assert "INSERT INTO suppressed_emails" in query
    assert "ON CONFLICT (message_id) DO NOTHING" in query
    assert "%s::jsonb" in query
    assert params[:8] == (
        "m1",
        "review",
        "P1",
        "Your bill",
        "billing@xfinity.com",
        "autopay already processed",
        "agent",
        None,
    )
    assert json.loads(params[8]) == [
        {"kind": "email", "ref": "m0", "note": "Thanks for your payment"}
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_task_index.py tests/test_repo_suppressions.py -q`
Expected: FAIL — missing attributes / module

- [ ] **Step 3: Implement**

Append to `repo/task_index.py`:

```python
_ROW_COLS = "task_gid, title, LEFT(notes, 300) AS notes, project, completed, due_on, permalink_url"


def substring_candidates(
    conn: Any, *, query: str, completed: bool | None, limit: int
) -> list[dict]:
    """Case-insensitive substring match over title + notes, newest first.
    Used by the triage agent's search_tasks tool (in-process, no API hop)."""
    where = ["(title ILIKE %s OR notes ILIKE %s)"]
    params: list = [f"%{query}%", f"%{query}%"]
    if completed is not None:
        where.append("completed = %s")
        params.append(completed)
    params.append(limit)
    return conn.execute(
        f"SELECT {_ROW_COLS} FROM task_index WHERE {' AND '.join(where)}"
        " ORDER BY updated_at DESC LIMIT %s",
        tuple(params),
    ).fetchall()


def get_rows(conn: Any, task_gids: list[str]) -> list[dict]:
    """Display rows for a set of GIDs (e.g. the semantic_candidates hits)."""
    if not task_gids:
        return []
    placeholders = ",".join(["%s"] * len(task_gids))
    return conn.execute(
        f"SELECT {_ROW_COLS} FROM task_index WHERE task_gid IN ({placeholders})",
        tuple(task_gids),
    ).fetchall()
```

New `repo/suppressions.py`:

```python
"""suppressed_emails — the audit trail for gate-2 decisions. Takes an open
connection. Best-effort by contract: callers log and move on if this fails."""

import json
from typing import Any


def insert(
    conn: Any,
    *,
    message_id: str,
    category: str,
    importance: str,
    subject: str | None,
    sender: str | None,
    reason: str,
    source: str,
    related_task_gid: str | None,
    evidence: list,
) -> None:
    conn.execute(
        """
        INSERT INTO suppressed_emails
            (message_id, category, importance, subject, sender, reason, source,
             related_task_gid, evidence)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (message_id) DO NOTHING
        """,
        (
            message_id,
            category,
            importance,
            subject,
            sender,
            reason,
            source,
            related_task_gid,
            json.dumps(evidence),
        ),
    )
```

Append to `repo/schema.sql`:

```sql

-- Gate-2 audit trail: emails that passed the policy gate but were judged
-- moot by the triage agent (source='agent') or the no-action phrase veto
-- (source='phrase'). related_task_gid is set when the email was attached
-- to an existing task as a comment instead. evidence = the agent's list.
CREATE TABLE IF NOT EXISTS suppressed_emails (
    message_id       TEXT PRIMARY KEY,
    category         TEXT NOT NULL,
    importance       TEXT NOT NULL,
    subject          TEXT,
    sender           TEXT,
    reason           TEXT NOT NULL,
    source           TEXT NOT NULL,
    related_task_gid TEXT,
    evidence         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_repo_task_index.py tests/test_repo_suppressions.py tests/test_repo.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add repo/task_index.py repo/suppressions.py repo/schema.sql tests/test_repo_task_index.py tests/test_repo_suppressions.py
git commit -m "feat: task_index lookups and suppressed_emails table"
```

---

### Task 4: Decision type, OTel instruments, and `claude.run_agent`

**Files:**
- Modify: `models/events.py` (append dataclass)
- Modify: `clients/otel.py`
- Modify: `clients/claude.py` (append)
- Test: `tests/test_claude_agent.py` (new), `tests/test_otel.py` (append one assertion)

**Interfaces:**
- Produces: `models.events.Decision` dataclass: `actionable: bool = True`, `reason: str = ""`, `related_task_gid: str | None = None`, `evidence: list = field(default_factory=list)`, `outcome: str = "actionable"` (one of `actionable | suppressed | attached | fail_open`).
- Produces: `clients.otel.tasks_suppressed` (Counter), `clients.otel.triage_duration` (Histogram, ms), `clients.otel.triage_tool_calls` (Counter) — no-ops until `setup_telemetry`.
- Produces: `clients.claude.AGENT_MODEL = "claude-sonnet-5"` and `clients.claude.run_agent(*, system: str, user: str, tools: list, output_schema: dict, max_iterations: int = 6, deadline_s: float = 60.0, request_timeout: float = 30.0) -> tuple[str | None, str]` returning `(final_text, stop)` with `stop in {"end_turn", "refusal", "max_iterations", "timeout"}`; `final_text` is the concatenated text blocks of the last assistant message when `stop == "end_turn"`, else `None`. Never swallows exceptions (caller owns fail-open).

- [ ] **Step 1: Write the failing tests**

`tests/test_claude_agent.py`:

```python
from types import SimpleNamespace

import clients.claude as claude


class _Msg(SimpleNamespace):
    pass


def _msg(stop_reason, text=None, in_tok=10, out_tok=5):
    content = [SimpleNamespace(type="text", text=text)] if text is not None else []
    return _Msg(
        stop_reason=stop_reason,
        content=content,
        usage=SimpleNamespace(input_tokens=in_tok, output_tokens=out_tok),
    )


class _FakeRunner:
    """Yields a scripted sequence of assistant messages like BetaToolRunner."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.consumed = 0

    def __iter__(self):
        for m in self._messages:
            self.consumed += 1
            yield m


def _install(monkeypatch, runner, captured):
    def fake_tool_runner(**kwargs):
        captured.update(kwargs)
        return runner

    fake_client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(tool_runner=fake_tool_runner))
    )
    monkeypatch.setattr(claude, "_get_client", lambda: fake_client)


def test_run_agent_returns_final_text_and_passes_params(monkeypatch):
    captured = {}
    runner = _FakeRunner([_msg("tool_use"), _msg("end_turn", text='{"actionable": true}')])
    _install(monkeypatch, runner, captured)
    text, stop = claude.run_agent(
        system="SYS", user="USER", tools=["t"], output_schema={"type": "object"}, max_iterations=4
    )
    assert (text, stop) == ('{"actionable": true}', "end_turn")
    assert captured["model"] == "claude-sonnet-5"
    assert captured["max_iterations"] == 4
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"]["effort"] == "medium"
    assert captured["output_config"]["format"] == {
        "type": "json_schema",
        "schema": {"type": "object"},
    }
    assert captured["system"][0]["text"] == "SYS"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["messages"] == [{"role": "user", "content": "USER"}]
    assert captured["tools"] == ["t"]
    assert captured["timeout"] == 30.0


def test_run_agent_refusal(monkeypatch):
    _install(monkeypatch, _FakeRunner([_msg("refusal")]), {})
    assert claude.run_agent(system="s", user="u", tools=[], output_schema={}) == (None, "refusal")


def test_run_agent_max_iterations_when_last_turn_still_wants_tools(monkeypatch):
    _install(monkeypatch, _FakeRunner([_msg("tool_use"), _msg("tool_use")]), {})
    assert claude.run_agent(system="s", user="u", tools=[], output_schema={}, max_iterations=2) == (
        None,
        "max_iterations",
    )


def test_run_agent_timeout_between_turns(monkeypatch):
    runner = _FakeRunner([_msg("tool_use"), _msg("end_turn", text="late")])
    _install(monkeypatch, runner, {})
    clock = iter([0.0, 100.0, 200.0, 300.0])
    monkeypatch.setattr(claude.time, "monotonic", lambda: next(clock))
    assert claude.run_agent(system="s", user="u", tools=[], output_schema={}, deadline_s=60) == (
        None,
        "timeout",
    )
    assert runner.consumed == 1  # stopped before asking for the second turn


def test_run_agent_records_token_usage(monkeypatch):
    spent = []
    monkeypatch.setattr(claude.otel.claude_tokens, "add", lambda n, attrs: spent.append((n, attrs)))
    _install(monkeypatch, _FakeRunner([_msg("end_turn", text="{}", in_tok=7, out_tok=3)]), {})
    claude.run_agent(system="s", user="u", tools=[], output_schema={})
    assert (7, {"token_type": "input"}) in spent
    assert (3, {"token_type": "output"}) in spent
```

Append to `tests/test_otel.py` (look at how the existing tests reference instruments — there is likely a test that asserts instruments are no-ops before setup; add alongside it):

```python
def test_triage_instruments_exist_as_noops():
    import clients.otel as otel

    for name in ("tasks_suppressed", "triage_duration", "triage_tool_calls"):
        assert hasattr(otel, name)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_claude_agent.py tests/test_otel.py -q`
Expected: FAIL — `run_agent` / instruments missing

- [ ] **Step 3: Implement**

Append to `models/events.py`:

```python
@dataclass
class Decision:
    """Gate-2 verdict from services/triage.py. Defaults ARE the fail-open
    state: actionable, no reason, no related task."""

    actionable: bool = True
    reason: str = ""
    related_task_gid: str | None = None
    evidence: list = field(default_factory=list)
    outcome: str = "actionable"  # actionable | suppressed | attached | fail_open
```

`clients/otel.py` — add three module-level no-op instruments next to `vertex_duration`:

```python
tasks_suppressed: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
triage_duration: metrics.Histogram = metrics.NoOpMeter("noop").create_histogram("noop")
triage_tool_calls: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

add them to the `global` line in `setup_telemetry`, and create them after `vertex_duration`:

```python
    tasks_suppressed = meter.create_counter(
        "asana.tasks_suppressed",
        description="Emails that passed the policy gate but were suppressed by gate 2",
    )
    triage_duration = meter.create_histogram(
        "asana.triage.duration", unit="ms", description="Triage agent wall-clock per email"
    )
    triage_tool_calls = meter.create_counter(
        "asana.triage.tool_calls", description="Triage agent tool invocations by tool"
    )
```

Append to `clients/claude.py` (add `import time` at the top):

```python
AGENT_MODEL = "claude-sonnet-5"


def run_agent(
    *,
    system: str,
    user: str,
    tools: list,
    output_schema: dict,
    max_iterations: int = 6,
    deadline_s: float = 60.0,
    request_timeout: float = 30.0,
) -> tuple[str | None, str]:
    """Run a tool-use loop with the SDK tool runner. Returns (final_text, stop)
    where stop is 'end_turn' | 'refusal' | 'max_iterations' | 'timeout'.
    final_text is the last assistant message's text (JSON per output_schema)
    only for 'end_turn'. Exceptions propagate — the caller owns fail-open."""
    runner = _get_client().beta.messages.tool_runner(
        model=AGENT_MODEL,
        max_tokens=2048,
        max_iterations=max_iterations,
        thinking={"type": "adaptive"},
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": output_schema},
        },
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        tools=tools,
        timeout=request_timeout,
    )
    started = time.monotonic()
    last = None
    for message in runner:
        last = message
        _record_usage(message)
        if message.stop_reason == "refusal":
            return None, "refusal"
        if message.stop_reason != "tool_use":
            break
        if time.monotonic() - started > deadline_s:
            return None, "timeout"
    if last is None or last.stop_reason == "tool_use":
        return None, "max_iterations"
    text = "".join(b.text for b in last.content if getattr(b, "type", None) == "text")
    return text, "end_turn"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_claude_agent.py tests/test_otel.py tests/test_events.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add models/events.py clients/otel.py clients/claude.py tests/test_claude_agent.py tests/test_otel.py
git commit -m "feat: Decision type, triage metrics, claude.run_agent tool loop"
```

---

### Task 5: Triage tools (`services/triage.py`, part 1)

**Files:**
- Create: `services/triage.py` (tools + helpers only; `decide()` comes in Task 6)
- Test: `tests/test_triage_tools.py`

**Interfaces:**
- Consumes: `clients.inbox_api.search/get_email` (Task 2), `repo.task_index.substring_candidates/get_rows/semantic_candidates`, `repo.tasks.get_gid_by_message`, `clients.vertex.embed`, `clients.asana.get_task_detail/get_stories`, `clients.db.get_conn`.
- Produces: module-level `CURRENT_MESSAGE_ID: contextvars.ContextVar[str | None]`; four `@beta_tool` objects `search_emails`, `get_email`, `search_tasks`, `get_task` (call with `.call({...})` in tests — `BetaFunctionTool.call(input: dict) -> str`); `TOOLS = [search_emails, get_email, search_tasks, get_task]`. Every tool returns a JSON string; on any backend failure it returns `{"error": "<ExceptionType>: <message>"}` and never raises. Each call increments `otel.triage_tool_calls` with `{"tool": <name>}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_triage_tools.py
import json

import clients.asana as asana
import clients.inbox_api as inbox_api
import clients.vertex as vertex
from repo import task_index as repo_index
from repo import tasks as repo_tasks
from services import triage
from tests.test_repo import FakeConn


def _j(tool, **kwargs):
    return json.loads(tool.call(kwargs))


# --- search_emails -----------------------------------------------------------


def test_search_emails_returns_trimmed_rows(monkeypatch):
    calls = []

    def fake_search(query, *, mode, limit, mailboxes=None):
        calls.append((query, mode, limit))
        return [
            {
                "message_id": "m9",
                "subject": "Thanks for your payment",
                "sender": "billing@xfinity.com",
                "sender_display": "Xfinity",
                "received_at": "2026-08-10T12:00:00Z",
                "preview": "Your automatic payment was processed",
                "web_link": "https://outlook/x",
                "mailbox": "me",
                "category": None,
                "importance": None,
            }
        ]

    monkeypatch.setattr(inbox_api, "search", fake_search)
    rows = _j(triage.search_emails, query="from:xfinity", limit=50)
    assert calls == [("from:xfinity", "graph", 25)]  # limit clamped to 25
    assert rows == [
        {
            "message_id": "m9",
            "subject": "Thanks for your payment",
            "sender": "billing@xfinity.com",
            "received_at": "2026-08-10T12:00:00Z",
            "preview": "Your automatic payment was processed",
            "category": None,
            "importance": None,
        }
    ]


def test_search_emails_error_is_returned_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("inbox-api down")

    monkeypatch.setattr(inbox_api, "search", boom)
    assert _j(triage.search_emails, query="x") == {"error": "RuntimeError: inbox-api down"}


# --- get_email ---------------------------------------------------------------


def test_get_email_returns_body_and_recipients(monkeypatch):
    monkeypatch.setattr(
        inbox_api,
        "get_email",
        lambda mid: {
            "id": mid,
            "subject": "Re: coaching",
            "from_email": "andrew@example.com",
            "from_name": "Andrew",
            "to": [{"name": "Ben", "address": "ben@drolet.cloud"}],
            "cc": [{"name": "Christy", "address": "christy@example.com"}],
            "received_at": "2026-08-14T16:16:00Z",
            "body": "<p>Ok thanks, Ben.</p>",
            "body_type": "html",
            "web_link": "https://outlook/m1",
        },
    )
    out = _j(triage.get_email, message_id="m1")
    assert out["subject"] == "Re: coaching"
    assert out["sender"] == "andrew@example.com"
    assert out["to"] == ["ben@drolet.cloud"]
    assert out["cc"] == ["christy@example.com"]
    assert out["body"] == "Ok thanks, Ben."  # html stripped


def test_get_email_caps_body(monkeypatch):
    monkeypatch.setattr(
        inbox_api,
        "get_email",
        lambda mid: {"body": "x" * 5000, "body_type": "text", "to": [], "cc": []},
    )
    assert len(_j(triage.get_email, message_id="m1")["body"]) == triage.EMAIL_BODY_CAP


# --- search_tasks ------------------------------------------------------------


def _stub_conn(monkeypatch, rows, message_gid=None):
    conn = FakeConn()
    monkeypatch.setattr(triage, "get_conn", lambda: conn)
    monkeypatch.setattr(repo_index, "substring_candidates", lambda c, **kw: rows)
    monkeypatch.setattr(
        repo_index, "get_rows", lambda c, gids: [r for r in rows if r["task_gid"] in gids]
    )
    monkeypatch.setattr(repo_tasks, "get_gid_by_message", lambda c, mid: message_gid)
    return conn


def test_search_tasks_substring_path(monkeypatch):
    _stub_conn(
        monkeypatch,
        [
            {
                "task_gid": "t1",
                "title": "Disney",
                "notes": "n",
                "completed": True,
                "due_on": None,
                "project": "P",
                "permalink_url": "u",
            }
        ],
    )
    out = _j(triage.search_tasks, query="disney", completed=None)
    assert out == [
        {
            "task_gid": "t1",
            "title": "Disney",
            "notes": "n",
            "completed": True,
            "due_on": None,
            "project": "P",
            "permalink_url": "u",
        }
    ]


def test_search_tasks_semantic_path_attaches_scores(monkeypatch):
    rows = [
        {
            "task_gid": "t1",
            "title": "Refund",
            "notes": "",
            "completed": False,
            "due_on": None,
            "project": None,
            "permalink_url": None,
        }
    ]
    _stub_conn(monkeypatch, rows)
    monkeypatch.setattr(vertex, "embed", lambda text, *, task_type: [0.1] * 768)
    monkeypatch.setattr(
        repo_index, "semantic_candidates", lambda c, **kw: [{"task_gid": "t1", "score": 0.91}]
    )
    out = _j(triage.search_tasks, query="disney refund", semantic=True)
    assert out[0]["task_gid"] == "t1" and out[0]["score"] == 0.91


def test_search_tasks_prepends_this_emails_existing_task(monkeypatch):
    rows = [
        {
            "task_gid": "t1",
            "title": "Other",
            "notes": "",
            "completed": False,
            "due_on": None,
            "project": None,
            "permalink_url": None,
        },
        {
            "task_gid": "t0",
            "title": "Same thread",
            "notes": "",
            "completed": False,
            "due_on": None,
            "project": None,
            "permalink_url": None,
        },
    ]
    _stub_conn(monkeypatch, rows, message_gid="t0")
    token = triage.CURRENT_MESSAGE_ID.set("msg-123")
    try:
        out = _j(triage.search_tasks, query="zzz")
    finally:
        triage.CURRENT_MESSAGE_ID.reset(token)
    assert out[0]["task_gid"] == "t0" and out[0]["same_email"] is True
    assert [r["task_gid"] for r in out].count("t0") == 1


def test_search_tasks_error_is_returned(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(triage, "get_conn", boom)
    assert _j(triage.search_tasks, query="x") == {"error": "RuntimeError: db down"}


# --- get_task ----------------------------------------------------------------


def test_get_task_returns_detail_and_comments(monkeypatch):
    monkeypatch.setattr(
        asana,
        "get_task_detail",
        lambda gid: {
            "gid": gid,
            "name": "Follow up on refund",
            "notes": "N" * 1000,
            "completed": True,
            "due_on": "2026-07-01",
            "permalink_url": "https://a/1",
        },
    )
    monkeypatch.setattr(
        asana,
        "get_stories",
        lambda gid: [
            {"type": "system", "text": "added to project", "created_at": "2026-06-01T00:00:00Z"},
            {
                "type": "comment",
                "text": "Refund landed",
                "created_at": "2026-07-02T00:00:00Z",
                "created_by": {"name": "Ben"},
            },
        ],
    )
    out = _j(triage.get_task, task_gid="1")
    assert out["name"] == "Follow up on refund"
    assert out["completed"] is True
    assert len(out["notes"]) == triage.TASK_NOTES_CAP
    assert out["comments"] == [
        {"text": "Refund landed", "created_at": "2026-07-02T00:00:00Z", "by": "Ben"}
    ]


def test_get_task_missing(monkeypatch):
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: None)
    assert _j(triage.get_task, task_gid="nope") == {"error": "not found"}


def test_tool_calls_are_counted(monkeypatch):
    counts = []
    monkeypatch.setattr(triage.otel.triage_tool_calls, "add", lambda n, attrs: counts.append(attrs))
    monkeypatch.setattr(inbox_api, "search", lambda *a, **k: [])
    triage.search_emails.call({"query": "x"})
    assert counts == [{"tool": "search_emails"}]


def test_tools_list_and_names():
    assert [t.name for t in triage.TOOLS] == [
        "search_emails",
        "get_email",
        "search_tasks",
        "get_task",
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_triage_tools.py -q`
Expected: FAIL — `ModuleNotFoundError: services.triage`

- [ ] **Step 3: Implement the tools**

```python
# services/triage.py
"""Gate 2 — the triage agent. Given a classified email, declared facts about
Ben (context/standing-context.md, `Roles`), and read-only search/fetch tools
over mail and tasks, decide whether the email still requires anything of him.
Mirrors what /searching-inbox-emails and /searching-tasks do by hand.

Design: docs/superpowers/specs/2026-08-18-standing-context-gate-design.md.
Every tool is a thin delegation to an existing client and returns a JSON
string; backend failures come back as {"error": ...} so one outage degrades
the agent's evidence instead of aborting the run. decide() is fail-open."""

import contextvars
import json
import logging
import re
from html.parser import HTMLParser

from anthropic import beta_tool

import clients.asana as asana
import clients.inbox_api as inbox_api
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from repo import task_index as repo_index
from repo import tasks as repo_tasks

logger = logging.getLogger(__name__)

CURRENT_MESSAGE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "triage_message_id", default=None
)

EMAIL_BODY_CAP = 2000
TASK_NOTES_CAP = 600
MAX_RESULTS = 25


def _err(exc: BaseException) -> str:
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


def _count(tool: str) -> None:
    otel.triage_tool_calls.add(1, {"tool": tool})


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    p = _TextOnly()
    try:
        p.feed(html)
    except Exception:
        return html
    return " ".join("".join(p.parts).split())


@beta_tool
def search_emails(query: str, mode: str = "graph", limit: int = 10) -> str:
    """Search Ben's mailboxes. Call this when the email describes a recurring
    obligation (a bill, renewal, subscription, payment) to see whether prior
    mail from the same sender shows it is already automated or already paid
    ("automatic payment", "thanks for your payment"), or when you need the
    rest of a thread. Returns a JSON list of {message_id, subject, sender,
    received_at, preview, category, importance}; pass a message_id to
    get_email for the full body.

    Args:
        query: In graph mode, Outlook KQL — `from:billing@xfinity.com`,
            `subject:refund`, or plain keywords. In db mode, plain keywords.
        mode: "graph" (live Outlook, all mailboxes — default) or "db"
            (already-processed mail, includes category/importance).
        limit: Max results, 1-25.
    """
    _count("search_emails")
    try:
        rows = inbox_api.search(query, mode=mode, limit=max(1, min(limit, MAX_RESULTS)))
    except Exception as exc:  # noqa: BLE001 — tool contract: never raise
        return _err(exc)
    keep = ("message_id", "subject", "sender", "received_at", "preview", "category", "importance")
    return json.dumps([{k: r.get(k) for k in keep} for r in rows], default=str)


@beta_tool
def get_email(message_id: str) -> str:
    """Fetch one email's full content by message_id (from search_emails or
    from the triage request). Use it to read a prior message's body when the
    preview is not enough. Returns JSON {subject, sender, to, cc, received_at,
    body} with the body capped at 2000 characters.

    Args:
        message_id: Graph message id.
    """
    _count("get_email")
    try:
        e = inbox_api.get_email(message_id)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    body = e.get("body") or ""
    if (e.get("body_type") or "").lower() == "html":
        body = _strip_html(body)
    return json.dumps(
        {
            "subject": e.get("subject"),
            "sender": e.get("from_email"),
            "to": [r.get("address") for r in e.get("to") or []],
            "cc": [r.get("address") for r in e.get("cc") or []],
            "received_at": e.get("received_at"),
            "body": body[:EMAIL_BODY_CAP],
        },
        default=str,
    )


@beta_tool
def search_tasks(
    query: str, semantic: bool = False, completed: bool | None = None, limit: int = 10
) -> str:
    """Search Ben's Asana tasks — open AND completed by default. Call this
    when the email could be a follow-up on something already tracked (a
    refund, a dispute, a thread that already produced a task) or when the
    email is a reply on a thread. Any task created from THIS email's thread is
    listed first with same_email=true. Returns a JSON list of {task_gid,
    title, notes, project, completed, due_on, permalink_url[, score]}; pass a
    task_gid to get_task for the description and comments.

    Args:
        query: Keywords (vendor, amount, subject words). With semantic=true,
            a natural-language description of the matter.
        semantic: true for nearest-neighbour ranking by meaning; false for
            substring match on title/notes.
        completed: null = both (default), false = open only, true = done only.
        limit: Max results, 1-25.
    """
    _count("search_tasks")
    limit = max(1, min(limit, MAX_RESULTS))
    try:
        with get_conn() as conn:
            if semantic:
                emb = vertex.embed(query, task_type="RETRIEVAL_QUERY")
                hits = repo_index.semantic_candidates(
                    conn,
                    query_embedding=emb,
                    completed=completed,
                    due_before=None,
                    due_after=None,
                    project=None,
                    limit=limit,
                )
                scores = {h["task_gid"]: h["score"] for h in hits}
                rows = repo_index.get_rows(conn, list(scores))
                for r in rows:
                    r["score"] = scores.get(r["task_gid"])
                rows.sort(key=lambda r: r["score"] or 0, reverse=True)
            else:
                rows = repo_index.substring_candidates(
                    conn, query=query, completed=completed, limit=limit
                )
            mid = CURRENT_MESSAGE_ID.get()
            own_gid = repo_tasks.get_gid_by_message(conn, mid) if mid else None
            if own_gid:
                own = [r for r in rows if r["task_gid"] == own_gid]
                rows = [r for r in rows if r["task_gid"] != own_gid]
                own_rows = own or repo_index.get_rows(conn, [own_gid])
                for r in own_rows:
                    r["same_email"] = True
                rows = own_rows + rows
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    return json.dumps([dict(r) for r in rows], default=str)


@beta_tool
def get_task(task_gid: str) -> str:
    """Fetch one Asana task's description, state, and recent comments by
    task_gid (from search_tasks). Call it before setting related_task_gid so
    you are sure it is the same matter. Returns JSON {name, notes, completed,
    due_on, permalink_url, comments:[{text, created_at, by}]}.

    Args:
        task_gid: Asana task GID.
    """
    _count("get_task")
    try:
        t = asana.get_task_detail(task_gid)
        if t is None:
            return json.dumps({"error": "not found"})
        stories = asana.get_stories(task_gid)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    comments = [
        {
            "text": s.get("text"),
            "created_at": s.get("created_at"),
            "by": (s.get("created_by") or {}).get("name"),
        }
        for s in stories
        if s.get("type") == "comment"
    ][-5:]
    return json.dumps(
        {
            "name": t.get("name"),
            "notes": (t.get("notes") or "")[:TASK_NOTES_CAP],
            "completed": bool(t.get("completed")),
            "due_on": t.get("due_on"),
            "permalink_url": t.get("permalink_url"),
            "comments": comments,
        },
        default=str,
    )


TOOLS = [search_emails, get_email, search_tasks, get_task]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_triage_tools.py -q`
Expected: all pass. If `beta_tool` rejects the `bool | None` annotation on `completed`, change it to `Optional[bool] = None` with `from typing import Optional` — the generated schema must allow null.

- [ ] **Step 5: Commit**

```bash
git add services/triage.py tests/test_triage_tools.py
git commit -m "feat: triage agent tools over mail and tasks"
```

---

### Task 6: `triage.decide()` — prompt, schema, parsing, fail-open

**Files:**
- Modify: `services/triage.py` (append)
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `clients.claude.run_agent` (Task 4), `services.standing_context.section` (Task 1), `models.events.Decision` (Task 4), `TOOLS`/`CURRENT_MESSAGE_ID` (Task 5).
- Produces: `services.triage.decide(event: EmailClassifiedEvent, *, today: str | None = None) -> Decision`; module constants `SYSTEM_PROMPT: str`, `OUTPUT_SCHEMA: dict`, `MAX_ITERATIONS = 6`, `DEADLINE_S = 60.0`, `BODY_CAP = 3000`; helper `build_user_message(event, *, today: str, roles: str) -> str`. `decide` never raises; it records `otel.triage_duration` in ms with `{"outcome": decision.outcome}`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_triage.py
import json

import clients.claude as claude
from models.events import Decision
from services import standing_context, triage
from tests.test_events import make_email_event


def _agent(monkeypatch, text=None, stop="end_turn", capture=None):
    def fake_run_agent(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if isinstance(text, BaseException):
            raise text
        return text, stop

    monkeypatch.setattr(claude, "run_agent", fake_run_agent)


def _roles(monkeypatch, body):
    monkeypatch.setattr(
        standing_context, "section", lambda name, **kw: body if name == "Roles" else ""
    )


def _ok(actionable=False, reason="coach fact applies", gid=None, evidence=None):
    return json.dumps(
        {
            "actionable": actionable,
            "reason": reason,
            "related_task_gid": gid,
            "evidence": evidence
            if evidence is not None
            else [{"kind": "fact", "ref": "Assistant Coach", "note": "resigned"}],
        }
    )


def test_suppression_decision_parsed(monkeypatch):
    _roles(monkeypatch, "### Assistant Coach\nEnded 2026-08-14.")
    captured = {}
    _agent(monkeypatch, _ok(), capture=captured)
    d = triage.decide(make_email_event(), today="2026-08-19")
    assert d == Decision(
        actionable=False,
        reason="coach fact applies",
        related_task_gid=None,
        evidence=[{"kind": "fact", "ref": "Assistant Coach", "note": "resigned"}],
        outcome="suppressed",
    )
    assert captured["tools"] is triage.TOOLS
    assert captured["output_schema"] is triage.OUTPUT_SCHEMA
    assert captured["max_iterations"] == triage.MAX_ITERATIONS
    assert captured["deadline_s"] == triage.DEADLINE_S
    assert captured["system"] == triage.SYSTEM_PROMPT


def test_user_message_has_date_roles_and_email(monkeypatch):
    _roles(monkeypatch, "### Assistant Coach\nEnded 2026-08-14.")
    captured = {}
    _agent(monkeypatch, _ok(True, "new request"), capture=captured)
    ev = make_email_event(cc=["x@example.com"], body="B" * 5000)
    triage.decide(ev, today="2026-08-19")
    user = captured["user"]
    assert "Today is 2026-08-19" in user
    assert "### Assistant Coach" in user
    assert "Subject: Quarterly report" in user
    assert "From: alice@example.com" in user
    assert "To: ben@drolet.cloud" in user
    assert "Cc: x@example.com" in user
    assert "review / P1" in user
    assert user.count("B") >= triage.BODY_CAP and user.count("B") < 5000


def test_user_message_omits_roles_block_when_empty(monkeypatch):
    _roles(monkeypatch, "")
    captured = {}
    _agent(monkeypatch, _ok(True, "x"), capture=captured)
    triage.decide(make_email_event(), today="2026-08-19")
    assert "Standing facts" not in captured["user"]


def test_related_task_outcome(monkeypatch):
    _roles(monkeypatch, "")
    _agent(monkeypatch, _ok(False, "same refund", gid="1217290596630525"))
    d = triage.decide(make_email_event())
    assert d.outcome == "attached" and d.related_task_gid == "1217290596630525"


def test_actionable_outcome(monkeypatch):
    _roles(monkeypatch, "")
    _agent(monkeypatch, _ok(True, "a real request"))
    assert triage.decide(make_email_event()).outcome == "actionable"


def test_urgent_short_circuits_without_calling_agent(monkeypatch):
    called = []
    monkeypatch.setattr(claude, "run_agent", lambda **kw: called.append(1) or (None, "end_turn"))
    d = triage.decide(make_email_event(category="urgent"))
    assert d == Decision() and called == []


def test_fail_open_paths(monkeypatch):
    _roles(monkeypatch, "")
    cases = [
        (RuntimeError("boom"), "end_turn"),
        (None, "refusal"),
        (None, "max_iterations"),
        (None, "timeout"),
        ("not json", "end_turn"),
        (json.dumps({"actionable": "maybe"}), "end_turn"),
        (_ok(False, ""), "end_turn"),  # suppression with empty reason
        (_ok(False, "   "), "end_turn"),
    ]
    for text, stop in cases:
        _agent(monkeypatch, text, stop)
        d = triage.decide(make_email_event())
        assert d.actionable is True and d.outcome == "fail_open", (text, stop)


def test_current_message_id_is_set_during_run_and_cleared_after(monkeypatch):
    _roles(monkeypatch, "")
    seen = {}

    def fake_run_agent(**kwargs):
        seen["mid"] = triage.CURRENT_MESSAGE_ID.get()
        return _ok(True, "x"), "end_turn"

    monkeypatch.setattr(claude, "run_agent", fake_run_agent)
    triage.decide(make_email_event(message_id="msg-777"))
    assert seen["mid"] == "msg-777"
    assert triage.CURRENT_MESSAGE_ID.get() is None


def test_duration_metric_recorded(monkeypatch):
    _roles(monkeypatch, "")
    recorded = []
    monkeypatch.setattr(
        triage.otel.triage_duration, "record", lambda ms, attrs: recorded.append(attrs)
    )
    _agent(monkeypatch, _ok(True, "x"))
    triage.decide(make_email_event())
    assert recorded == [{"outcome": "actionable"}]


def test_output_schema_is_strict_object():
    s = triage.OUTPUT_SCHEMA
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert set(s["required"]) == {"actionable", "reason", "related_task_gid", "evidence"}


def test_system_prompt_carries_the_rules():
    p = triage.SYSTEM_PROMPT
    for needle in (
        "ONLY",
        "period",
        "cc",
        "no action required",
        "automatic payment",
        "related_task_gid",
        "evidence",
        "Action required",
    ):
        assert needle in p, needle
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_triage.py -q`
Expected: FAIL — `decide` missing

- [ ] **Step 3: Implement `decide`** (append to `services/triage.py`; add `import time`, `from datetime import date`, `import clients.claude as claude`, `from models.events import Decision, EmailClassifiedEvent`, `from services import standing_context` to the imports)

```python
MAX_ITERATIONS = 6
DEADLINE_S = 60.0
BODY_CAP = 3000

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean"},
        "reason": {
            "type": "string",
            "description": "One sentence naming the fact, email, or task that decided it.",
        },
        "related_task_gid": {
            "type": ["string", "null"],
            "description": "GID of an existing task covering the same matter, else null.",
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["email", "task", "fact", "thread"]},
                    "ref": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["kind", "ref", "note"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["actionable", "reason", "related_task_gid", "evidence"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You triage Ben's email for his task list. An email has already been classified as task-worthy on its own terms. Your job is to decide whether, given everything you can find out, it still requires anything of Ben. You have read-only tools to search and read his mail and his Asana tasks; use them when the email's shape calls for it (recurring bills and renewals → prior mail from the sender; refunds, disputes, follow-ups, replies → existing tasks), and skip them when the email is plainly a new request.

Rules:
- Set actionable to false ONLY when evidence you actually have — a standing fact that currently applies, prior mail, an existing task, or the thread itself — clearly shows the email requires nothing of Ben. If unsure, actionable is true.
- A standing fact that states a period applies only inside that period; compare against today's date.
- A reply to a thread Ben started, or a message where Ben is only on cc and the content is acknowledgment ("thanks for letting us know", "sounds good", "best wishes"), is not a task.
- "no action required", "no action is needed", "for your records", "automatic payment", "autopay", "will be charged automatically" — in this email or in prior mail from the same sender — disqualifies a payment or review task.
- The sender's schedule (a statement posted, a payment series ending, a feature auto-enabling on a date) is not Ben's deadline. Broadcast vendor announcements default to not actionable. Never treat "Action required" as present unless the source says it.
- If an existing task covers the same matter — same vendor/amount/instrument within small variance, same thread, same saga — set related_task_gid to it, open or completed, but only after you have fetched it (get_task) or seen enough of it to be sure. If the email shows a closed matter has regressed, leave related_task_gid null and set actionable true.
- Name every lookup or fact you relied on in evidence (kind: email|task|fact|thread; ref: message_id, task gid, fact heading, or "thread"; note: what it showed).
- Do not reason beyond the facts given and the evidence you retrieved.

Respond with the JSON object only."""


def build_user_message(event: EmailClassifiedEvent, *, today: str, roles: str) -> str:
    parts = [f"Today is {today}."]
    if roles:
        parts.append(
            "Standing facts about Ben (some state the period they apply to; a fact "
            "whose period has passed does not apply):\n\n" + roles
        )
    parts.append(
        "The email:\n"
        f"Subject: {event.get('subject') or ''}\n"
        f"From: {event.get('sender') or ''}"
        + (f" ({event['sender_display']})" if event.get("sender_display") else "")
        + "\n"
        f"To: {', '.join(event.get('to') or [])}\n"
        f"Cc: {', '.join(event.get('cc') or [])}\n"
        f"Received: {event.get('received_at') or ''}\n"
        f"Classified: {event.get('category')} / {event.get('importance')}\n"
        f"Message id: {event.get('message_id')}\n\n"
        f"{(event.get('body') or '')[:BODY_CAP]}"
    )
    return "\n\n".join(parts)


def _fail_open(reason: str, message_id: str) -> Decision:
    logger.warning("triage fail-open (%s) message_id=%s", reason, message_id)
    return Decision(outcome="fail_open")


def _parse(text: str | None, stop: str, message_id: str) -> Decision:
    if stop != "end_turn" or not text:
        return _fail_open(stop, message_id)
    try:
        data = json.loads(text)
    except ValueError:
        return _fail_open("unparseable", message_id)
    if not isinstance(data, dict) or not isinstance(data.get("actionable"), bool):
        return _fail_open("schema", message_id)
    reason = str(data.get("reason") or "").strip()
    gid = data.get("related_task_gid") or None
    evidence = data.get("evidence") if isinstance(data.get("evidence"), list) else []
    actionable = data["actionable"]
    if gid is not None:
        return Decision(
            actionable=False,
            reason=reason,
            related_task_gid=str(gid),
            evidence=evidence,
            outcome="attached",
        )
    if actionable:
        return Decision(actionable=True, reason=reason, evidence=evidence, outcome="actionable")
    if not reason:
        return _fail_open("no_reason", message_id)
    return Decision(actionable=False, reason=reason, evidence=evidence, outcome="suppressed")


def decide(event: EmailClassifiedEvent, *, today: str | None = None) -> Decision:
    """Gate 2. Never raises; every failure returns the actionable default."""
    if event.get("category") == "urgent":
        return Decision()
    message_id = event["message_id"]
    today = today or date.today().isoformat()
    roles = standing_context.section("Roles")
    user = build_user_message(event, today=today, roles=roles)
    token = CURRENT_MESSAGE_ID.set(message_id)
    t0 = time.monotonic()
    try:
        text, stop = claude.run_agent(
            system=SYSTEM_PROMPT,
            user=user,
            tools=TOOLS,
            output_schema=OUTPUT_SCHEMA,
            max_iterations=MAX_ITERATIONS,
            deadline_s=DEADLINE_S,
        )
        decision = _parse(text, stop, message_id)
    except Exception:  # noqa: BLE001 — fail-open by contract
        logger.exception("triage agent failed message_id=%s", message_id)
        decision = Decision(outcome="fail_open")
    finally:
        CURRENT_MESSAGE_ID.reset(token)
    otel.triage_duration.record((time.monotonic() - t0) * 1000, {"outcome": decision.outcome})
    logger.info(
        "triage outcome=%s actionable=%s related=%s message_id=%s reason=%s",
        decision.outcome,
        decision.actionable,
        decision.related_task_gid,
        message_id,
        decision.reason,
    )
    return decision
```

Note for the implementer: `Decision` is a dataclass, so `d == Decision(...)` in the tests compares fields — keep the field set exactly as in Task 4.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_triage.py tests/test_triage_tools.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add services/triage.py tests/test_triage.py
git commit -m "feat: triage.decide — prompt, strict schema, fail-open parsing"
```

---

### Task 7: Phrase veto and Calendar in deadline extraction

**Files:**
- Modify: `services/policy.py`
- Modify: `services/deadline.py`
- Test: `tests/test_policy.py` (append), `tests/test_deadline.py` (append)

**Interfaces:**
- Produces: `services.policy.no_action_phrase(key_points: list[str]) -> str | None` — the first matched phrase (lowercased, as matched), or `None`. Module constant `NO_ACTION_PATTERNS` (list of compiled regexes).
- Modifies: `services.deadline.extract_deadline` prompt gains a `Calendar facts:` block (from `standing_context.section("Calendar")`) before `Today is ...` when non-empty. Return contract unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_policy.py`:

```python
@pytest.mark.parametrize(
    ("points", "expected"),
    [
        (
            ["Final payment will be sent; no action required unless you cancel"],
            "no action required",
        ),
        (["No action is needed"], "no action is needed"),
        (["Statement attached for your records"], "for your records"),
        (["Your automatic payment of $70 will draw on 9/9"], "automatic payment"),
        (["Enrolled in autopay"], "autopay"),
        (["Pay the $70 bill by 9/9"], None),
        ([], None),
    ],
)
def test_no_action_phrase(points, expected):
    assert policy.no_action_phrase(points) == expected
```

Append to `tests/test_deadline.py`:

```python
from services import standing_context


def test_calendar_section_reaches_prompt(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        claude, "extract", lambda prompt: captured.setdefault("p", prompt) and "null"
    )
    monkeypatch.setattr(
        standing_context,
        "section",
        lambda name, **kw: (
            "- SFUSD fall term: 2026-08-17 to 2026-12-18." if name == "Calendar" else ""
        ),
    )
    deadline.extract_deadline(make_email_event())
    p = captured["p"]
    assert "Calendar facts:" in p and "SFUSD fall term" in p
    assert p.index("SFUSD") < p.index("Today is")


def test_empty_calendar_leaves_prompt_unchanged(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        claude, "extract", lambda prompt: captured.setdefault("p", prompt) and "null"
    )
    monkeypatch.setattr(standing_context, "section", lambda name, **kw: "")
    deadline.extract_deadline(make_email_event())
    assert captured["p"].startswith("Today is ")
    assert "Calendar facts" not in captured["p"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_policy.py tests/test_deadline.py -q`
Expected: FAIL

- [ ] **Step 3: Implement**

`services/policy.py` — add `import re` and:

```python
# Deterministic backstop for gate 2 (docs/no_action_needed_example.md): if the
# enrichment's own key points contain an explicit no-action phrase, do not
# create the task. Free to test; catches what the agent missed when the
# summarizer wrote the disqualifier down anyway.
NO_ACTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"no action (?:is )?(?:required|needed)",
        r"for your records",
        r"automatic payment",
        r"\bautopay\b",
    )
]


def no_action_phrase(key_points: list[str]) -> str | None:
    """The first no-action phrase found in the generated key points, else None."""
    for point in key_points:
        for pat in NO_ACTION_PATTERNS:
            m = pat.search(point or "")
            if m:
                return m.group(0).lower()
    return None
```

`services/deadline.py` — add `from services import standing_context` and build the prompt as:

```python
    calendar = standing_context.section("Calendar")
    preamble = f"Calendar facts:\n{calendar}\n\n" if calendar else ""
    prompt = (
        f"{preamble}"
        f"Today is {today}.\n"
        ...  # rest unchanged
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_policy.py tests/test_deadline.py -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add services/policy.py services/deadline.py tests/test_policy.py tests/test_deadline.py
git commit -m "feat: no-action phrase veto; Calendar facts in deadline prompt"
```

---

### Task 8: Handler integration

**Files:**
- Modify: `handlers/task_create.py`
- Test: `tests/test_task_create.py` (append)

**Interfaces:**
- Consumes: `triage.decide` (Task 6), `policy.no_action_phrase` (Task 7), `repo.suppressions.insert` (Task 3), `asana.create_story`, `otel.tasks_suppressed` (Task 4).
- Produces: handler behaviour per spec table; a private `_suppress(event, *, reason, source, related_task_gid, evidence) -> None` helper that posts the related-task comment (when a GID is given), writes the row, and increments the counter — each step best-effort.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_task_create.py`)

Also add this autouse fixture near the top of the file so the pre-existing
`test_handle_*` tests never reach the real agent (they would otherwise hit
`claude.run_agent` → missing API key → fail-open, which passes but logs a
traceback per test):

```python
import pytest


@pytest.fixture(autouse=True)
def _default_triage(monkeypatch):
    monkeypatch.setattr(triage, "decide", lambda event, **kw: Decision())
```

Then the new tests:

```python
from models.events import Decision
from repo import suppressions as repo_suppressions
from services import triage


def _stub_triage(monkeypatch, decision):
    calls = []

    def fake_decide(event, **kw):
        calls.append(event)
        return decision

    monkeypatch.setattr(triage, "decide", fake_decide)
    return calls


def _stub_suppressions(monkeypatch):
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    return rows


def _stub_story(monkeypatch, fail=False):
    stories = []

    def fake_story(task_gid, *, text=None, html_text=None):
        if fail:
            raise RuntimeError("asana down")
        stories.append((task_gid, text))
        return {"gid": "s1"}

    monkeypatch.setattr(asana, "create_story", fake_story)
    return stories


def _count_suppressed(monkeypatch):
    counts = []
    monkeypatch.setattr(
        task_create.otel.tasks_suppressed, "add", lambda n, attrs: counts.append(attrs)
    )
    return counts


def test_suppressed_decision_creates_nothing_and_records(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    summary_calls, _ = _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    _stub_triage(
        monkeypatch,
        Decision(
            actionable=False,
            reason="coach fact",
            evidence=[{"kind": "fact", "ref": "Coach", "note": "x"}],
            outcome="suppressed",
        ),
    )
    task_create.handle(make_email_event())
    assert created == {} and summary_calls == []
    assert rows == [
        {
            "message_id": "msg-123",
            "category": "review",
            "importance": "P1",
            "subject": "Quarterly report",
            "sender": "alice@example.com",
            "reason": "coach fact",
            "source": "agent",
            "related_task_gid": None,
            "evidence": [{"kind": "fact", "ref": "Coach", "note": "x"}],
        }
    ]
    assert counts == [
        {"category": "review", "importance": "P1", "source": "agent", "attached": "false"}
    ]


def test_attached_decision_comments_on_related_task(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    stories = _stub_story(monkeypatch)
    _stub_triage(
        monkeypatch,
        Decision(actionable=False, reason="same refund", related_task_gid="t9", outcome="attached"),
    )
    task_create.handle(make_email_event())
    assert created == {}
    assert stories == [
        ("t9", "Related email: Quarterly report — same refund — https://outlook.example/msg-123")
    ]
    assert rows[0]["related_task_gid"] == "t9" and rows[0]["source"] == "agent"
    assert counts[0]["attached"] == "true"


def test_story_failure_does_not_resurrect_task(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    _stub_story(monkeypatch, fail=True)
    _stub_triage(
        monkeypatch,
        Decision(actionable=False, reason="r", related_task_gid="t9", outcome="attached"),
    )
    task_create.handle(make_email_event())
    assert created == {} and len(rows) == 1


def test_suppression_row_failure_does_not_resurrect_task(monkeypatch):
    _stub_db(monkeypatch)

    def boom(conn, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo_suppressions, "insert", boom)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    _stub_triage(monkeypatch, Decision(actionable=False, reason="r", outcome="suppressed"))
    task_create.handle(make_email_event())  # must not raise
    assert created == {}


def test_actionable_decision_proceeds_unchanged(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    summary_calls, _ = _stub_enrichment(monkeypatch, key_points=["Pay the bill"])
    created = _capture_create(monkeypatch)
    calls = _stub_triage(monkeypatch, Decision(actionable=True, reason="new", outcome="actionable"))
    task_create.handle(make_email_event())
    assert len(calls) == 1 and len(summary_calls) == 1
    assert created["html_notes"] and rows == []


def test_phrase_veto_after_summary(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    _stub_enrichment(monkeypatch, key_points=["Final payment will be sent; no action required"])
    created = _capture_create(monkeypatch)
    _stub_triage(monkeypatch, Decision(actionable=True, reason="looked fine", outcome="actionable"))
    task_create.handle(make_email_event())
    assert created == {}
    assert rows[0]["source"] == "phrase" and rows[0]["reason"] == "no action required"
    assert counts[0]["source"] == "phrase"


def test_gate1_still_runs_before_triage(monkeypatch):
    calls = _stub_triage(monkeypatch, Decision())
    created = _capture_create(monkeypatch)
    task_create.handle(make_email_event(category="reference"))
    assert calls == [] and created == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_task_create.py -q`
Expected: new tests FAIL (triage not called, no suppression path)

- [ ] **Step 3: Implement** — edit `handlers/task_create.py`

Imports: add `from repo import suppressions as repo_suppressions` and `triage` to the `from services import ...` line, and `from models.events import Decision, EmailClassifiedEvent`.

Add the helper:

```python
def _suppress(
    event: EmailClassifiedEvent,
    *,
    reason: str,
    source: str,
    related_task_gid: str | None,
    evidence: list,
) -> None:
    """Gate-2 outcome: no task. Optionally attach the email to a related task
    as a comment, then record. Every step is best-effort — the decision was
    made on evidence and a recording failure never reverses it."""
    if related_task_gid:
        try:
            asana.create_story(
                related_task_gid,
                text=f"Related email: {event['subject']} — {reason} — {event.get('web_link') or ''}".rstrip(
                    " —"
                ),
            )
        except Exception:
            logger.exception(
                "related-task comment failed gid=%s message_id=%s",
                related_task_gid,
                event["message_id"],
            )
    try:
        with get_conn() as conn:
            repo_suppressions.insert(
                conn,
                message_id=event["message_id"],
                category=event["category"],
                importance=event["importance"],
                subject=event.get("subject"),
                sender=event.get("sender"),
                reason=reason,
                source=source,
                related_task_gid=related_task_gid,
                evidence=evidence,
            )
    except Exception:
        logger.exception("suppressed_emails insert failed message_id=%s", event["message_id"])
    otel.tasks_suppressed.add(
        1,
        {
            "category": event["category"],
            "importance": event["importance"],
            "source": source,
            "attached": "true" if related_task_gid else "false",
        },
    )
    logger.info(
        "Task suppressed source=%s related=%s message_id=%s reason=%s",
        source,
        related_task_gid,
        event["message_id"],
        reason,
    )
```

In `handle`, after the gate-1 block and before `summary = email_summary.generate(event)`:

```python
    decision: Decision = triage.decide(event)
    if not decision.actionable or decision.related_task_gid:
        _suppress(
            event,
            reason=decision.reason,
            source="agent",
            related_task_gid=decision.related_task_gid,
            evidence=decision.evidence,
        )
        return
```

Right after `summary = email_summary.generate(event)`:

```python
    phrase = policy.no_action_phrase(summary.key_points)
    if phrase:
        _suppress(event, reason=phrase, source="phrase", related_task_gid=None, evidence=[])
        return
```

Note: the comment text in the test expects exactly `Related email: {subject} — {reason} — {web_link}`; the `.rstrip(" —")` only trims when `web_link` is empty.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass (existing `test_handle_*` tests still pass because `triage.decide` must be stubbed in them — if any existing test now calls the real `decide`, it will hit `claude.run_agent` → `_get_client()` → missing `ANTHROPIC_API_KEY` → caught by fail-open and the task is created, so they should still pass; if one fails, add `_stub_triage(monkeypatch, Decision())` to it).

- [ ] **Step 5: Commit**

```bash
git add handlers/task_create.py tests/test_task_create.py
git commit -m "feat: gate 2 in task_create — triage, suppression record, related-task comment, phrase veto"
```

---

### Task 9: Deploy headroom, dependency pin, docs

**Files:**
- Modify: `requirements.txt` (anthropic pin)
- Modify: `terraform/cloud_functions.tf` (tasks-events `timeout_seconds`, line ~99)
- Modify: `CLAUDE.md` (Task policy section)
- Modify: `docs/more_context_needed.md`, `docs/no_action_needed_example.md` (one-line pointer each)

**Interfaces:** none.

- [ ] **Step 1: Pin the SDK**

In `requirements.txt` change `anthropic>=0.25` to `anthropic>=0.116` (tool runner + `@beta_tool` with structured output). Run `.venv/bin/pip install -r requirements.txt -q` and confirm `.venv/bin/python -c "from anthropic import beta_tool"` works.

- [ ] **Step 2: Raise the events CF timeout**

In `terraform/cloud_functions.tf`, the **first** `timeout_seconds = 120` (the `tasks-events` function, under the block whose `max_instance_count = 3` appears at line ~98) becomes `timeout_seconds = 300`, with a trailing comment `# triage agent: ≤60s decide + enrichment + Asana`. Leave the webhook function's timeout at 120. Run `terraform -chdir=terraform fmt` if terraform is installed; do not plan/apply.

- [ ] **Step 3: Update CLAUDE.md "Task policy"**

Replace the paragraph under `## Task policy` with:

```markdown
`services/policy.py::warrants_task` — urgent/review/respond → task (gate 1).
Then `services/triage.py::decide` (gate 2) — a Sonnet 5 tool-runner agent with
read-only `search_emails` / `get_email` / `search_tasks` / `get_task` tools
that reads the `Roles` section of `context/standing-context.md` and decides
whether the email still requires anything; non-actionable emails are recorded
in `suppressed_emails` (optionally attached to a related task as a comment)
and never created. Fail-open everywhere; `urgent` skips gate 2. A
deterministic no-action-phrase veto (`policy.no_action_phrase`) runs on the
Haiku key points as a backstop. Changing what becomes a task is a change HERE
(or in `context/standing-context.md` for declared facts — that directory is in
the deploy allowlist), never an inbox deploy. Enrichment (summary via Claude
Haiku, deadline extraction for P0/P1 via Sonnet — the latter reads the
`Calendar` section) runs only for events that pass both gates. Design:
`docs/superpowers/specs/2026-08-18-standing-context-gate-design.md`.
```

- [ ] **Step 4: Pointers in the evidence docs**

Append to the top of `docs/more_context_needed.md` (after the first paragraph) and `docs/no_action_needed_example.md` (after the first paragraph):

```markdown
> **Status (2026-08-19):** addressed by gate 2 — see
> `docs/superpowers/specs/2026-08-18-standing-context-gate-design.md` and
> `services/triage.py`. Keep logging new cases here; they are the eval set.
```

- [ ] **Step 5: Run everything**

Run: `.venv/bin/pytest tests/ -q` and `.venv/bin/ruff check .` (if installed)
Expected: all pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt terraform/cloud_functions.tf CLAUDE.md docs/more_context_needed.md docs/no_action_needed_example.md
git commit -m "chore: pin anthropic>=0.116, events CF timeout 300s, docs for gate 2"
```

---

## After the plan: manual verification (not a subagent task)

With `scripts/fetch-env.sh` run and `.env` sourced, run `triage.decide` against the real cases listed in the spec's Testing section (the three Micro Admin emails must suppress on the coach fact; the two `Lee@sfvikings.com` invites must survive; Xfinity must suppress on prior payment mail; Disney must attach to a completed task; the coaching-thread replies must suppress; Zelle/PayPal must suppress; Google Meet must not be "Action required"; the Micro Admin emails must stop being suppressed with `today="2027-01-05"`). Use `/verifying-pr-locally` for the mechanics and post results to the PR. Then run `scripts/migrate_db.py` after the terraform apply so `suppressed_emails` exists before the CF deploys.

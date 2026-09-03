# Tasks-Owned Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move gate 1 off inbox's `category`. A Haiku screener owned by this repo returns a three-way verdict — `task` / `relate` / `drop` — reading attachment metadata inbox's classifier never sees; `relate` emails become a comment on the open task they report on instead of a silent drop.

**Architecture:** `services/screening.py` mirrors `services/triage.py` (gate 2): one entry point, never raises, fail-open by contract. It runs on every `email_classified` event, replacing the `warrants_task` set-membership test, which is retained as the *outage* fallback. `task` goes to the unchanged Sonnet triage agent; `relate` goes to a new `services/relating.py` (embedding nearest-neighbour → similarity floor → one Haiku confirm → Asana verify) which reaches `_suppress()`'s existing related-task comment branch; `drop` records a suppression row. Verification is an offline dry run over ~1,200 historical emails.

**Tech Stack:** Python 3.13, Anthropic SDK (`claude-haiku-4-5`, structured outputs via `output_config.format`), Vertex AI `gemini-embedding-001`, pgvector, OpenTelemetry, Cloud SQL Postgres via `google.cloud.sql.connector` (backtest harness only), pytest.

**Spec:** `docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md` (as amended 2026-08-28)

## Global Constraints

- Layer rules (`CLAUDE.md`): `clients/` is I/O only; `services/` is business logic with no direct HTTP; `handlers/` orchestrate and are called only from `main.py`; `models/` imports from no other layer.
- Every mailbox read goes through `clients/inbox_api.py`. Never import MSAL or call Graph directly.
- Gate 1, the relating stage, and gate 2 are all **fail-open**: no exception from any of them may crash an event.
- DB writes in handlers are **best-effort** — Asana is the source of truth.
- Ruff line-length 100. Run `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` before each commit.
- Test command: `.venv/bin/pytest tests/ -q`
- Model id is `claude-haiku-4-5` — no date suffix. (`clients/claude.py::summarize` pins `claude-haiku-4-5-20251001`; pre-existing, out of scope.)
- Priority vocabulary is exactly `P0`, `P1`, `P2`, `P3`. Verdict vocabulary is exactly `task`, `relate`, `drop`.
- **Nothing in this plan ever closes a task.** The relate path comments; Ben closes.
- Never commit a declared fact to this repo.

## Amendment note

The spec was amended 2026-08-28 after this plan's first draft. Two decisions **reversed**, and implementers who read the earlier plan must not carry them forward:

| Was | Now | Where |
|---|---|---|
| `Screening.is_task: bool` | `Screening.verdict: str` (three-way), `is_task` becomes a property | Task 1 |
| Keep the urgent bypass keyed on `category == "urgent"` | **Delete the bypass entirely** | Task 8 |

New since the first draft: `services/relating.py` (Task 6), the shared unfetchable-gid helper (Task 5), the `task_content.py` action-item `source` fix (Task 9), and `relate`-aware scoring in the harness (Task 10).

---

### Task 1: Event fields and the `Screening` / `Match` types

`inbox` already publishes `graph_message_id` and `has_attachments` (`~/src/inbox/services/email_events.py:74-76`); `models/events.py` never declared them. Both are `NotRequired` because events published before this lands, and every existing test fixture, omit them.

**Files:**
- Modify: `models/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Consumes: nothing
- Produces: `EmailClassifiedEvent["graph_message_id"]: NotRequired[str]`, `EmailClassifiedEvent["has_attachments"]: NotRequired[bool]`, `Screening(verdict: str = "task", priority: str = "P2", reason: str = "", outcome: str = "task")` with an `is_task` property, and `Match(task_gid: str | None = None, resolves: bool = False, reason: str = "", evidence: list = [])`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_events.py`:

```python
def test_email_event_graph_fields_optional():
    event = make_email_event()
    assert event.get("graph_message_id") is None
    assert event.get("has_attachments") is None
    with_att = make_email_event(graph_message_id="AAMkAGI0", has_attachments=True)
    assert with_att["graph_message_id"] == "AAMkAGI0"
    assert with_att["has_attachments"] is True


def test_screening_defaults_are_the_fail_open_state():
    from models.events import Screening

    s = Screening()
    assert s.verdict == "task"
    assert s.is_task is True
    assert s.priority == "P2"
    assert s.reason == ""
    assert s.outcome == "task"


def test_screening_is_task_tracks_the_verdict():
    from models.events import Screening

    assert Screening(verdict="task").is_task is True
    assert Screening(verdict="relate").is_task is False
    assert Screening(verdict="drop").is_task is False


def test_match_defaults_are_no_match():
    from models.events import Match

    m = Match()
    assert m.task_gid is None
    assert m.resolves is False
    assert m.reason == ""
    assert m.evidence == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_events.py -q`
Expected: FAIL with `ImportError: cannot import name 'Screening' from 'models.events'`

- [ ] **Step 3: Write minimal implementation**

In `models/events.py`, add two fields to `EmailClassifiedEvent` immediately after `seed_links`:

```python
    # Published by inbox since the schedule extraction; declared here so the
    # screener can reach attachments. graph_message_id is the IMMUTABLE GRAPH
    # id — message_id is inbox's internal UUID and inbox-api rejects it with
    # ErrorInvalidIdMalformed.
    graph_message_id: NotRequired[str]
    has_attachments: NotRequired[bool]
```

Then append at the end of the file:

```python
@dataclass
class Screening:
    """Gate-1 verdict from services/screening.py.

    Three-way by design: `drop` and `relate` are both "no task", but they are
    not the same answer. `relate` means the email needs no work of its own AND
    plausibly reports on something already tracked — a confirmation, a receipt,
    a "your request was processed". services/relating.py turns that into a
    comment. Collapsing the two loses the only path to _suppress()'s
    related-task branch.

    Defaults ARE the fail-open state: it is a task, at middling priority."""

    verdict: str = "task"  # task | relate | drop
    priority: str = "P2"  # P0 | P1 | P2 | P3
    reason: str = ""
    outcome: str = "task"  # task | relate | drop | fail_open

    @property
    def is_task(self) -> bool:
        return self.verdict == "task"


@dataclass
class Match:
    """Result of services/relating.py. Defaults ARE the no-match state, which
    is a normal outcome: the email is recorded as a plain suppression and no
    comment is posted."""

    task_gid: str | None = None
    resolves: bool = False
    reason: str = ""
    evidence: list = field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_events.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models/events.py tests/test_events.py
git commit -m "feat: declare graph fields and the three-way Screening / Match types"
```

---

### Task 2: `clients/claude.py::classify` and two metrics

**Files:**
- Modify: `clients/claude.py`
- Modify: `clients/otel.py`
- Test: `tests/test_claude_agent.py`, `tests/test_otel.py`

**Interfaces:**
- Consumes: nothing
- Produces: `claude.classify(*, system: str, user: str, schema: dict) -> str` (returns raw JSON text; **raises** on API failure — callers own fail-open), `otel.tasks_screened`, `otel.tasks_related`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_claude_agent.py`:

```python
def test_classify_sends_schema_and_returns_text(monkeypatch):
    import clients.claude as claude

    captured = {}

    class _Block:
        type = "text"
        text = '  {"verdict": "task"}  '

    class _Resp:
        content = [_Block()]

        class usage:
            input_tokens = 10
            output_tokens = 2

    class _Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return _Resp()

    class _Client:
        messages = _Messages()

    monkeypatch.setattr(claude, "_get_client", lambda: _Client())

    out = claude.classify(system="SYS", user="USER", schema={"type": "object"})

    assert out == '{"verdict": "task"}'
    assert captured["model"] == "claude-haiku-4-5"
    assert captured["system"][0]["text"] == "SYS"
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["messages"] == [{"role": "user", "content": "USER"}]
    assert captured["output_config"]["format"]["schema"] == {"type": "object"}
```

Append to `tests/test_otel.py`:

```python
def test_screening_and_relating_counters_exist():
    import clients.otel as otel

    otel.tasks_screened.add(1, {"outcome": "relate", "priority": "P3"})
    otel.tasks_related.add(1, {"matched": "false"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_claude_agent.py tests/test_otel.py -q`
Expected: FAIL with `AttributeError: module 'clients.claude' has no attribute 'classify'`

- [ ] **Step 3: Write minimal implementation**

In `clients/claude.py`, add after `summarize`:

```python
def classify(*, system: str, user: str, schema: dict) -> str:
    """Single-turn structured classification. Haiku, temperature 0,
    max_tokens 256. Returns the raw JSON text produced under `schema`.

    Used by both gate-1 stages: services/screening.py (the verdict) and
    services/relating.py (the match confirm).

    Raises on any API failure — each caller owns its own fail-open behaviour,
    and swallowing here would hide an outage behind a default verdict."""
    response = _get_client().messages.create(  # type: ignore[call-overload]
        model="claude-haiku-4-5",
        max_tokens=256,
        temperature=0,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user}],
    )
    _record_usage(response)
    return "".join(
        b.text  # type: ignore[union-attr]
        for b in response.content
        if getattr(b, "type", None) == "text"
    ).strip()
```

In `clients/otel.py`:

1. Add beside the other no-op defaults (near line 35):

```python
tasks_screened: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
tasks_related: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

2. Extend the `global` declaration on line 46:

```python
    global tasks_suppressed, triage_duration, triage_tool_calls
    global tasks_screened, tasks_related
```

3. Add after the `triage_tool_calls` creation:

```python
    tasks_screened = meter.create_counter(
        "asana.tasks_screened",
        description="Gate-1 verdicts by outcome (task|relate|drop|fail_open) and priority",
    )
    tasks_related = meter.create_counter(
        "asana.tasks_related",
        description="relate verdicts that did and did not find an open task",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_claude_agent.py tests/test_otel.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clients/claude.py clients/otel.py tests/test_claude_agent.py tests/test_otel.py
git commit -m "feat: claude.classify helper and screening/relating metrics"
```

---

### Task 3: Screening prompt construction

Pure functions — no network. `attachment_lines` is the one exception and is fully stubbed in tests.

**Files:**
- Create: `services/screening.py`
- Test: `tests/test_screening.py`

**Interfaces:**
- Consumes: `models.events.EmailClassifiedEvent`, `clients.inbox_api.get_attachments`
- Produces: `screening.SYSTEM_PROMPT: str`, `screening.OUTPUT_SCHEMA: dict`, `screening.BODY_CAP: int`, `screening.VERDICTS: tuple`, `screening.attachment_lines(event) -> list[str]`, `screening.build_user_message(event, *, today: str, roles: str, attachments: list[str]) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_screening.py`:

```python
import clients.inbox_api as inbox_api
from services import screening
from tests.test_events import make_email_event


def _attachments(monkeypatch, payload):
    def fake(gid):
        if isinstance(payload, BaseException):
            raise payload
        return payload

    monkeypatch.setattr(inbox_api, "get_attachments", fake)


def test_attachment_lines_skipped_when_flag_absent(monkeypatch):
    _attachments(monkeypatch, AssertionError("must not be called"))
    assert screening.attachment_lines(make_email_event()) == []


def test_attachment_lines_formats_name_type_size(monkeypatch):
    _attachments(
        monkeypatch,
        {
            "attachments": [
                {"name": "Checking.csv", "content_type": "text/csv", "size": 65903},
                {"name": "logo.png", "content_type": "image/png", "size": 40, "is_inline": True},
            ]
        },
    )
    event = make_email_event(has_attachments=True, graph_message_id="AAMk")
    assert screening.attachment_lines(event) == ["Checking.csv  text/csv  64.4 KB"]


def test_attachment_lines_uses_graph_id_not_message_id(monkeypatch):
    seen = []
    monkeypatch.setattr(inbox_api, "get_attachments", lambda gid: seen.append(gid) or {})
    screening.attachment_lines(
        make_email_event(message_id="uuid-form", graph_message_id="AAMkGRAPH", has_attachments=True)
    )
    assert seen == ["AAMkGRAPH"]


def test_attachment_lines_missing_graph_id_is_not_an_error(monkeypatch):
    _attachments(monkeypatch, AssertionError("must not be called"))
    assert screening.attachment_lines(make_email_event(has_attachments=True)) == []


def test_attachment_lines_swallows_backend_failure(monkeypatch):
    _attachments(monkeypatch, RuntimeError("inbox-api down"))
    event = make_email_event(has_attachments=True, graph_message_id="AAMk")
    assert screening.attachment_lines(event) == []


def test_build_user_message_includes_facts_headers_and_attachments():
    event = make_email_event(subject="checking and saving", body="")
    msg = screening.build_user_message(
        event,
        today="2026-08-24",
        roles="Ben handles Dana's finances.",
        attachments=["Checking.csv  text/csv  64.4 KB"],
    )
    assert "Today is 2026-08-24." in msg
    assert "Ben handles Dana's finances." in msg
    assert "Subject: checking and saving" in msg
    assert "Attachments:" in msg
    assert "Checking.csv  text/csv  64.4 KB" in msg
    assert "(empty body)" in msg


def test_build_user_message_caps_body():
    event = make_email_event(body="x" * 5000)
    msg = screening.build_user_message(event, today="2026-08-24", roles="", attachments=[])
    assert "x" * screening.BODY_CAP in msg
    assert "x" * (screening.BODY_CAP + 1) not in msg


def test_output_schema_is_three_way():
    assert screening.VERDICTS == ("task", "relate", "drop")
    assert screening.OUTPUT_SCHEMA["properties"]["verdict"]["enum"] == ["task", "relate", "drop"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_screening.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.screening'`

- [ ] **Step 3: Write minimal implementation**

Create `services/screening.py`:

```python
"""Gate 1 — the screener. Every email inbox processes arrives here, whatever
category it was filed under. Given the email, its attachment metadata, and the
declared facts about Ben (`Roles`), return one of three verdicts.

    task    needs work of its own      → gate 2 (services/triage.py)
    relate  needs no work, but plausibly reports on something already tracked
                                       → services/relating.py → a comment
    drop    neither                    → a suppressed_emails row

`relate` is not a softer `drop`. Without it the only way a confirmation could
reach _suppress()'s related-task branch is for this gate to be WRONG in a
convenient direction, and the dry run scores that as a precision failure.

This replaces the category set-membership test in services/policy.py, which
survives as the OUTAGE fallback: see screen(). Gate 2 is the precision stage —
this one is deliberately over-inclusive.

Design: docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md."""

import json
import logging
import time
from datetime import date

import clients.claude as claude
import clients.inbox_api as inbox_api
import clients.otel as otel
from models.events import EmailClassifiedEvent, Screening
from services import policy, standing_context

logger = logging.getLogger(__name__)

BODY_CAP = 2000
ATTACHMENT_CAP = 10
PRIORITIES = ("P0", "P1", "P2", "P3")
VERDICTS = ("task", "relate", "drop")

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "reason": {
            "type": "string",
            "description": "One sentence naming what decided it.",
        },
    },
    "required": ["verdict", "priority", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You screen Ben's email for his task list. Every email he receives reaches you — personal mail, bills, newsletters, receipts, automated notices. Return one of three verdicts, plus how much the matter is worth.

- "task": the email asks something of Ben or carries an obligation he would want on a list.
- "relate": the email needs no work of its own, but it plausibly reports on something already being tracked — a booking confirmation, a receipt, a delivery notice, a "your request has been processed", a reply closing a loop. You do NOT need to know which task; a later stage searches for it, and finding nothing is a normal outcome.
- "drop": neither. Nothing is asked, and it reports on nothing Ben would be tracking.

This is a first-pass filter. A later stage with search tools checks whether a "task" is already handled, so that is not your job.

Rules:
- When in doubt between "task" and anything else, choose "task". A spurious task costs seconds to close; a swallowed message about a bill, a child's schooling, or a family member's care is unbounded.
- When in doubt between "relate" and "drop", choose "relate". A match is still required downstream, so a wrong "relate" costs one embedding and one cheap call — never a spurious task.
- Judge the email, not the sender's type. Marketing from a vendor Ben uses is not a task; a short personal note from a family member usually is.
- An empty or near-empty body is not evidence of nothing. Read the subject, the sender, and the attachment names. A bare message with documents attached is usually someone handing Ben something to deal with.
- Attachments that look like records, statements, forms, or reports (.csv, .pdf, .xlsx, .docx) sent by a person rather than a system are a strong signal of "task".
- Typical "relate": confirmations of a booking, order, payment, appointment, or application; shipping and delivery notices for something ordered; "we received your request"; a receipt for a transaction Ben initiated.
- Typical "drop": newsletters, marketing, social and app notifications, digests, promotional offers, automated security notices requiring nothing.
- Typical "task": a failed or declined payment, an expiring card, a document awaiting signature, a stated deadline, a request for information, a question waiting on an answer, a form to complete.
- Use the standing facts to work out whose business this is. A fact that states a period applies only inside that period — compare against today's date.

Priority is stakes, independent of urgency:
- P0: critical — major consequence if missed; health, finances, legal standing, or a key relationship.
- P1: a real obligation or meaningful opportunity; it will matter if ignored.
- P2: worthwhile but not essential.
- P3: minor, low-stakes, or purely informational.

Set priority even for "relate" and "drop"; it is ignored in those cases.

Respond with the JSON object only."""


def _fmt_size(n: int | None) -> str:
    if not n:
        return "unknown size"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def attachment_lines(event: EmailClassifiedEvent) -> list[str]:
    """Names, content types, and sizes of non-inline attachments.

    Reads graph_message_id, NOT message_id: message_id is inbox's internal
    UUID and inbox-api answers it with ErrorInvalidIdMalformed (HTTP 502).

    Best-effort by construction — any failure yields []. Attachment metadata
    is a bonus signal, never a reason to fail a screening."""
    if not event.get("has_attachments"):
        return []
    graph_id = event.get("graph_message_id")
    if not graph_id:
        return []
    try:
        items = inbox_api.get_attachments(graph_id).get("attachments") or []
    except Exception:  # noqa: BLE001 — bonus signal; never fatal
        logger.warning("attachment fetch failed message_id=%s", event.get("message_id"))
        return []
    lines: list[str] = []
    for item in items:
        if item.get("is_inline"):
            continue
        name = (item.get("name") or "(unnamed)").strip()
        kind = item.get("content_type") or "unknown"
        lines.append(f"{name}  {kind}  {_fmt_size(item.get('size'))}")
        if len(lines) >= ATTACHMENT_CAP:
            break
    return lines


def build_user_message(
    event: EmailClassifiedEvent, *, today: str, roles: str, attachments: list[str]
) -> str:
    parts = [f"Today is {today}."]
    if roles:
        parts.append(
            "Standing facts about Ben (a fact that states a period applies only "
            "inside that period):\n\n" + roles
        )
    sender = event.get("sender") or ""
    if event.get("sender_display"):
        sender = f"{sender} ({event['sender_display']})"
    lines = [
        "The email:",
        f"Subject: {event.get('subject') or '(no subject)'}",
        f"From: {sender}",
        f"To: {', '.join(event.get('to') or [])}",
        f"Cc: {', '.join(event.get('cc') or [])}",
        f"Received: {event.get('received_at') or ''}",
    ]
    if attachments:
        lines.append("Attachments:")
        lines.extend(f"  {line}" for line in attachments)
    body = (event.get("body") or "").strip()
    lines.append("")
    lines.append(body[:BODY_CAP] if body else "(empty body)")
    parts.append("\n".join(lines))
    return "\n\n".join(parts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_screening.py -q`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add services/screening.py tests/test_screening.py
git commit -m "feat: screening prompt construction with the three-way verdict"
```

---

### Task 4: `screen()` — the verdict, with fail-open to `warrants_task`

The critical behaviour: a Claude outage must degrade to *today's* rule. Fail-opening to `task` would produce ~23 junk tasks/day during an outage. And the fallback may **never** return `relate` — that is a judgement it cannot make.

**Files:**
- Modify: `services/screening.py`, `services/policy.py` (docstring only)
- Test: `tests/test_screening.py`

**Interfaces:**
- Consumes: `claude.classify`, `policy.warrants_task`, `standing_context.section`, `otel.tasks_screened`
- Produces: `screening.screen(event, *, today: str | None = None) -> Screening`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_screening.py`:

```python
import json

import clients.claude as claude
from services import standing_context


def _classify(monkeypatch, payload, capture=None):
    def fake(*, system, user, schema):
        if capture is not None:
            capture.update(system=system, user=user, schema=schema)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    monkeypatch.setattr(claude, "classify", fake)


def _no_attachments(monkeypatch):
    monkeypatch.setattr(screening, "attachment_lines", lambda event: [])


def _roles(monkeypatch, body=""):
    monkeypatch.setattr(
        standing_context, "section", lambda name, **kw: body if name == "Roles" else ""
    )


def _verdict(verdict="task", priority="P1", reason="because"):
    return json.dumps({"verdict": verdict, "priority": priority, "reason": reason})


def test_screen_accepts_a_task_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("task", "P1", "bank statements attached"))
    result = screening.screen(make_email_event(category="ignore"), today="2026-08-24")
    assert result.verdict == "task"
    assert result.is_task is True
    assert result.priority == "P1"
    assert result.reason == "bank statements attached"
    assert result.outcome == "task"


def test_screen_accepts_a_relate_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("relate", "P1", "rental car confirmation"))
    result = screening.screen(make_email_event(category="reference"), today="2026-08-27")
    assert result.verdict == "relate"
    assert result.is_task is False
    assert result.outcome == "relate"


def test_screen_accepts_a_drop_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("drop", "P3", "marketing newsletter"))
    result = screening.screen(make_email_event(), today="2026-08-24")
    assert result.verdict == "drop"
    assert result.outcome == "drop"


def test_screen_passes_facts_and_email_to_the_model(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch, "Ben handles Dana's finances.")
    captured = {}
    _classify(monkeypatch, _verdict(), capture=captured)
    screening.screen(make_email_event(subject="checking and saving"), today="2026-08-24")
    assert captured["system"] == screening.SYSTEM_PROMPT
    assert captured["schema"] == screening.OUTPUT_SCHEMA
    assert "Ben handles Dana's finances." in captured["user"]
    assert "checking and saving" in captured["user"]


def test_screen_falls_back_to_warrants_task_on_api_failure(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, RuntimeError("anthropic down"))

    rescued = screening.screen(make_email_event(category="review", importance="P1"))
    assert rescued.verdict == "task"
    assert rescued.priority == "P1"
    assert rescued.outcome == "fail_open"

    dropped = screening.screen(make_email_event(category="ignore", importance="P3"))
    assert dropped.verdict == "drop"
    assert dropped.outcome == "fail_open"


def test_fallback_never_returns_relate(monkeypatch):
    """relate is a judgement the fallback cannot make; it must not guess one."""
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, RuntimeError("down"))
    verdicts = [
        screening.screen(make_email_event(category=c)).verdict
        for c in ("urgent", "review", "respond", "reference", "ignore")
    ]
    assert verdicts == ["task", "task", "task", "drop", "drop"]


def test_screen_falls_back_on_unparseable_output(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, "not json at all")
    result = screening.screen(make_email_event(category="ignore"))
    assert result.outcome == "fail_open"
    assert result.verdict == "drop"


def test_screen_falls_back_on_an_unknown_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("maybe", "P1", "x"))
    assert screening.screen(make_email_event(category="ignore")).outcome == "fail_open"


def test_screen_coerces_an_unknown_priority(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("task", "URGENT", "x"))
    assert screening.screen(make_email_event()).priority == "P2"


def test_screen_fallback_coerces_a_bad_inbox_importance(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, RuntimeError("down"))
    result = screening.screen(make_email_event(category="review", importance="high"))
    assert result.priority == "P2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_screening.py -q`
Expected: FAIL with `AttributeError: module 'services.screening' has no attribute 'screen'`

- [ ] **Step 3: Write minimal implementation**

Append to `services/screening.py`:

```python
def _fallback(event: EmailClassifiedEvent) -> Screening:
    """Outage degradation. Falls back to the pre-screening rule so a Claude
    failure reproduces today's behaviour exactly — noisy-but-known, never a
    flood. Fail-opening to `task` would create a task for every email that
    arrived during the outage.

    Never returns `relate`: whether an email reports on an open task is a
    judgement this fallback has no way to make, and guessing it would send
    mail to the relating stage on no evidence."""
    priority = event.get("importance") or "P2"
    return Screening(
        verdict="task" if policy.warrants_task(event) else "drop",
        priority=priority if priority in PRIORITIES else "P2",
        reason="screening unavailable — fell back to the inbox category",
        outcome="fail_open",
    )


def _parse(raw: str) -> Screening:
    """Raises on anything malformed; screen() turns that into _fallback."""
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("screening output was not an object")
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    priority = data.get("priority")
    if priority not in PRIORITIES:
        priority = "P2"
    return Screening(
        verdict=verdict,
        priority=priority,
        reason=str(data.get("reason") or "").strip(),
        outcome=verdict,
    )


def screen(event: EmailClassifiedEvent, *, today: str | None = None) -> Screening:
    """Gate 1. Never raises. Every failure path lands on _fallback."""
    message_id = event.get("message_id", "")
    t0 = time.monotonic()
    try:
        user = build_user_message(
            event,
            today=today or date.today().isoformat(),
            roles=standing_context.section("Roles"),
            attachments=attachment_lines(event),
        )
        verdict = _parse(
            claude.classify(system=SYSTEM_PROMPT, user=user, schema=OUTPUT_SCHEMA)
        )
    except Exception:  # noqa: BLE001 — fail-open by contract
        logger.exception("screening failed message_id=%s", message_id)
        verdict = _fallback(event)
    otel.tasks_screened.add(1, {"outcome": verdict.outcome, "priority": verdict.priority})
    logger.info(
        "screening verdict=%s outcome=%s priority=%s elapsed_ms=%d message_id=%s reason=%s",
        verdict.verdict,
        verdict.outcome,
        verdict.priority,
        int((time.monotonic() - t0) * 1000),
        message_id,
        verdict.reason,
    )
    return verdict
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_screening.py -q`
Expected: PASS (18 tests)

- [ ] **Step 5: Retitle `services/policy.py` as the fallback it now is**

Its module docstring still claims ownership of the decision. Replace the docstring (lines 1-5) with:

```python
"""Task policy backstops.

`warrants_task` was gate 1 until services/screening.py took over. It is
retained as that gate's OUTAGE FALLBACK: when the screener cannot reach
Claude, screening._fallback calls this so the pipeline degrades to the old
category rule instead of marking every arriving email a task. Do not call it
as a gate — screening.screen is the gate.

`no_action_phrase` is unchanged: a deterministic backstop that runs AFTER
enrichment, on the Haiku key points."""
```

Leave the code alone — `tests/test_policy.py::test_warrants_task` must keep passing untouched, which is the point.

- [ ] **Step 6: Run the policy tests**

Run: `.venv/bin/pytest tests/test_policy.py -q`
Expected: PASS, unchanged.

- [ ] **Step 7: Commit**

```bash
git add services/screening.py services/policy.py tests/test_screening.py
git commit -m "feat: screening verdict with fail-open to the inbox-category rule"
```

---

### Task 5: Shared gid guard, section default, title fallback move

Three small changes. The gid guard is currently `services/triage.py::_gid_exists`; the relating stage needs the same check, and the spec calls for lifting it rather than writing it twice. It belongs in `clients/asana.py` — it is one HTTP call and a boolean.

Note `urgent` section behaviour is preserved: it maps to `ASANA_SECTION_URGENT_GID`, which is optional, so an unset var still yields `None`.

**Files:**
- Modify: `clients/asana.py` (add `task_exists`, drop the importance branch at :112)
- Modify: `services/sections.py`
- Test: `tests/test_asana_client.py`, `tests/test_sections.py`

**Interfaces:**
- Consumes: `asana.get_task_detail`
- Produces: `asana.task_exists(task_gid: str) -> bool`; `sections.for_category` defaults unmapped categories to `ASANA_SECTION_REVIEW_GID`; `asana.create_task` no longer reads `event["importance"]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_asana_client.py`:

```python
def test_task_exists_is_true_when_fetchable(monkeypatch):
    import clients.asana as asana

    monkeypatch.setattr(asana, "get_task_detail", lambda gid: {"name": "x"})
    assert asana.task_exists("42") is True


def test_task_exists_is_false_when_absent_or_erroring(monkeypatch):
    import clients.asana as asana

    monkeypatch.setattr(asana, "get_task_detail", lambda gid: None)
    assert asana.task_exists("42") is False

    def boom(gid):
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "get_task_detail", boom)
    assert asana.task_exists("42") is False


def test_create_task_does_not_read_importance(monkeypatch):
    """The [PX] prefix is the handler's business; clients/ is I/O only."""
    import clients.asana as asana
    from tests.test_events import make_email_event

    captured = {}

    class _Resp:
        status_code = 201

        @staticmethod
        def json():
            return {"data": {"gid": "42", "permalink_url": "https://a/42"}}

        @staticmethod
        def raise_for_status():
            return None

    monkeypatch.setattr(asana, "ASANA_API_KEY", "k")
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "p")
    monkeypatch.setattr(asana, "_request", lambda *a, **kw: captured.update(kw) or _Resp())

    asana.create_task(make_email_event(importance="P0", subject="Quarterly report"))
    assert captured["json"]["data"]["name"] == "Quarterly report"
```

Append to `tests/test_sections.py`:

```python
def test_rescued_categories_default_to_the_review_section(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    assert sections.for_category("ignore") == "sec-review"
    assert sections.for_category("reference") == "sec-review"
    assert sections.for_category("") == "sec-review"


def test_urgent_still_unsectioned_when_its_gid_is_unset(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    monkeypatch.delenv("ASANA_SECTION_URGENT_GID", raising=False)
    assert sections.for_category("urgent") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sections.py tests/test_asana_client.py -q`
Expected: FAIL — `module 'clients.asana' has no attribute 'task_exists'`

- [ ] **Step 3: Write minimal implementation**

In `clients/asana.py`, add after `get_task_detail`:

```python
def task_exists(task_gid: str) -> bool:
    """Whether a GID names a task we can actually fetch.

    Shared by gate 2 (services/triage.py) and the relate path
    (services/relating.py): both treat an unfetchable gid as no match, so any
    failure counts as 'cannot fetch' rather than propagating."""
    try:
        return get_task_detail(task_gid) is not None
    except Exception:  # noqa: BLE001 — unfetchable is unfetchable
        logger.warning("task %s not fetchable — treating as no match", task_gid)
        return False
```

Replace the `"name"` line (line 112) and its comment:

```python
        # Title is always built by the caller (handlers/task_create.py), which
        # owns the [PX] prefix. Standard: "Title" section of
        # docs/task-content-standard.md (authoritative — doc wins).
        "name": title or event["subject"] or "(no subject)",
```

In `services/sections.py`, replace `for_category`:

```python
# Categories with no section of their own — an email the gate-1 screener
# rescued from ignore/reference — land in Review rather than unsectioned.
_DEFAULT_SECTION_VAR = "ASANA_SECTION_REVIEW_GID"


def for_category(category: str) -> str | None:
    var = _BY_CATEGORY.get(category, _DEFAULT_SECTION_VAR)
    return os.environ.get(var) or None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sections.py tests/test_asana_client.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clients/asana.py services/sections.py tests/test_asana_client.py tests/test_sections.py
git commit -m "feat: shared task_exists guard, Review default section, [PX] out of the asana client"
```

---

### Task 6: `services/relating.py` — find the open task a `relate` email reports on

Cost discipline is the whole constraint: `relate` lands on the receipt/confirmation population, which is large, so a Sonnet tool-runner per email is not affordable. Everything needed already exists — no new infrastructure.

The similarity floor is the precision knob. A comment on the **wrong** task is read as fact about that task and has no cheap recovery, so it starts conservative and the dry run tunes it.

**Files:**
- Create: `services/relating.py`
- Test: `tests/test_relating.py`

**Interfaces:**
- Consumes: `clients.vertex.embed`, `repo.task_index.semantic_candidates` / `get_rows`, `clients.db.get_conn`, `clients.claude.classify`, `clients.asana.task_exists`, `otel.tasks_related`, `models.events.Match`
- Produces: `relating.match(event) -> Match`, `relating.SIMILARITY_FLOOR: float`, `relating.CANDIDATES: int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_relating.py`:

```python
import json

import clients.asana as asana
import clients.claude as claude
import clients.vertex as vertex
from models.events import Match
from repo import task_index as repo_index
from services import relating
from tests.test_events import make_email_event
from tests.test_repo import FakeConn


def _stub_corpus(monkeypatch, rows, embed_error=None):
    """rows: list of (task_gid, title, score)."""

    def fake_embed(text, *, task_type):
        if embed_error:
            raise embed_error
        return [0.1] * 768

    monkeypatch.setattr(vertex, "embed", fake_embed)
    monkeypatch.setattr(relating, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        repo_index,
        "semantic_candidates",
        lambda conn, **kw: [{"task_gid": g, "score": s} for g, _, s in rows],
    )
    monkeypatch.setattr(
        repo_index,
        "get_rows",
        lambda conn, gids: [
            {"task_gid": g, "title": t, "notes": "", "due_on": None}
            for g, t, _ in rows
            if g in gids
        ],
    )


def _stub_confirm(monkeypatch, payload, capture=None):
    def fake(*, system, user, schema):
        if capture is not None:
            capture.update(system=system, user=user, schema=schema)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    monkeypatch.setattr(claude, "classify", fake)


def _answer(task_gid=None, resolves=False, reason="x"):
    return json.dumps({"task_gid": task_gid, "resolves": resolves, "reason": reason})


def test_match_below_the_floor_never_calls_the_model(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car", relating.SIMILARITY_FLOOR - 0.01)])
    _stub_confirm(monkeypatch, AssertionError("must not be called"))
    result = relating.match(make_email_event())
    assert result == Match(reason="no open task above the similarity floor")


def test_match_with_no_candidates_at_all(monkeypatch):
    _stub_corpus(monkeypatch, [])
    _stub_confirm(monkeypatch, AssertionError("must not be called"))
    assert relating.match(make_email_event()).task_gid is None


def test_match_confirms_and_verifies(monkeypatch):
    _stub_corpus(monkeypatch, [("1217730397662201", "Book rental car for Ohio trip", 0.83)])
    _stub_confirm(monkeypatch, _answer("1217730397662201", True, "same reservation"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)

    result = relating.match(make_email_event(subject="Confirmed: Enterprise Reservation 12345678"))

    assert result.task_gid == "1217730397662201"
    assert result.resolves is True
    assert result.reason == "same reservation"
    assert result.evidence == [
        {"kind": "task", "ref": "1217730397662201", "note": "same reservation"}
    ]


def test_null_is_a_normal_answer(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Something adjacent", 0.72)])
    _stub_confirm(monkeypatch, _answer(None, False, "only adjacent"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)
    result = relating.match(make_email_event())
    assert result.task_gid is None
    assert result.reason == "only adjacent"


def test_unfetchable_gid_is_treated_as_no_match(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car", 0.9)])
    _stub_confirm(monkeypatch, _answer("111", True, "same matter"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: False)
    assert relating.match(make_email_event()).task_gid is None


def test_a_gid_outside_the_candidates_is_rejected(monkeypatch):
    """Guards against a hallucinated gid landing a comment on an unrelated task."""
    _stub_corpus(monkeypatch, [("111", "Book rental car", 0.9)])
    _stub_confirm(monkeypatch, _answer("999999", True, "made up"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)
    assert relating.match(make_email_event()).task_gid is None


def test_candidate_titles_reach_the_confirm_prompt(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car for Ohio trip", 0.83)])
    captured = {}
    _stub_confirm(monkeypatch, _answer(None), capture=captured)
    relating.match(make_email_event(subject="Confirmed: Enterprise Reservation"))
    assert "Book rental car for Ohio trip" in captured["user"]
    assert "111" in captured["user"]
    assert "Confirmed: Enterprise Reservation" in captured["user"]


def test_embedding_failure_degrades_to_no_match(monkeypatch):
    _stub_corpus(monkeypatch, [], embed_error=RuntimeError("vertex down"))
    _stub_confirm(monkeypatch, AssertionError("must not be called"))
    assert relating.match(make_email_event()).task_gid is None


def test_confirm_failure_degrades_to_no_match(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car", 0.9)])
    _stub_confirm(monkeypatch, RuntimeError("anthropic down"))
    assert relating.match(make_email_event()).task_gid is None


def test_match_only_searches_open_tasks(monkeypatch):
    captured = {}
    monkeypatch.setattr(vertex, "embed", lambda text, *, task_type: [0.1] * 768)
    monkeypatch.setattr(relating, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        repo_index, "semantic_candidates", lambda conn, **kw: captured.update(kw) or []
    )
    monkeypatch.setattr(repo_index, "get_rows", lambda conn, gids: [])
    relating.match(make_email_event())
    assert captured["completed"] is False
    assert captured["limit"] == relating.CANDIDATES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_relating.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.relating'`

- [ ] **Step 3: Write minimal implementation**

Create `services/relating.py`:

```python
"""The `relate` outcome — find the open task an email reports on.

Gate 1 says a task MAY exist; this stage finds it or gives up. Cost discipline
is the constraint: `relate` lands on the receipt/confirmation population, which
is large, so a Sonnet tool-runner per email is not affordable. Instead:
nearest-neighbour over the embedding corpus that already exists
(services/task_index.py keeps it fresh), a similarity floor, one Haiku call to
confirm, and an Asana fetch to verify whatever it names.

No match is a NORMAL outcome, not a failure. Match() falls through to a plain
suppressed_emails row, which is still strictly better than the single log line
these emails leave today.

This stage never closes a task. It supplies the gid; handlers/task_create.py
::_suppress renders the comment, and its wording asks Ben to close.

Design: docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md."""

import json
import logging

import clients.asana as asana
import clients.claude as claude
import clients.otel as otel
import clients.vertex as vertex
from clients.db import get_conn
from models.events import EmailClassifiedEvent, Match
from repo import task_index as repo_index

logger = logging.getLogger(__name__)

BODY_CAP = 2000
NOTES_CAP = 300
CANDIDATES = 3

# Cosine score below which the best neighbour is not worth a confirm call.
# This is the precision knob. A comment on the WRONG task is read as fact
# about that task and has no cheap recovery, so it starts conservative; the
# dry run (scripts/backtest_screening.py) tunes it against real neighbours.
SIMILARITY_FLOOR = 0.55

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "task_gid": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "One of the candidate gids, verbatim, or null.",
        },
        "resolves": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["task_gid", "resolves", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are matching an email to an open Asana task. The email has already been judged to need no work of its own; the only question is whether it reports on something Ben is already tracking.

You are shown up to three candidate tasks, nearest first by meaning. They are candidates, not answers — they are the closest things in the corpus, which is not the same as being about the same matter.

Rules:
- Return a task_gid ONLY when the email and the task are the same matter: same vendor, same reservation or order number, same amount, same instrument, same thread, same saga. "Both are about travel" is not the same matter.
- null is the expected answer. Most emails of this kind match nothing. Returning null costs nothing; naming the wrong task puts a false statement on a real task, which Ben reads as fact and which has no cheap undo. When the candidates are merely adjacent, return null.
- task_gid must be one of the candidate gids shown, copied verbatim. Never invent one.
- Set resolves to true only when this email IS the task's resolution — the thing the task was waiting for has happened: the booking is confirmed, the refund landed, the payment cleared, the reply arrived. Set it false when the email merely concerns the same matter: another notice in the saga, a duplicate, a status update that changes nothing. Ben reads resolves as "close this task", so do not claim a resolution you have not seen evidence of.
- reason is one sentence naming what made it the same matter, or why nothing matched.

Respond with the JSON object only."""


def _candidates(event: EmailClassifiedEvent) -> list[dict]:
    """Top open-task neighbours by cosine similarity, best first."""
    text = f"{event.get('subject') or ''}\n\n{(event.get('body') or '')[:BODY_CAP]}"
    embedding = vertex.embed(text, task_type="RETRIEVAL_QUERY")
    with get_conn() as conn:
        hits = repo_index.semantic_candidates(
            conn,
            query_embedding=embedding,
            completed=False,  # a completed task needs no "looks resolved" comment
            due_before=None,
            due_after=None,
            project=None,
            limit=CANDIDATES,
        )
        scores = {h["task_gid"]: h["score"] for h in hits}
        rows = repo_index.get_rows(conn, list(scores))
    for row in rows:
        row["score"] = scores.get(row["task_gid"]) or 0.0
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def build_user_message(event: EmailClassifiedEvent, rows: list[dict]) -> str:
    body = (event.get("body") or "").strip()
    parts = [
        "The email:",
        f"Subject: {event.get('subject') or '(no subject)'}",
        f"From: {event.get('sender') or ''}",
        f"Received: {event.get('received_at') or ''}",
        "",
        body[:BODY_CAP] if body else "(empty body)",
        "",
        "Open tasks that may be the same matter:",
    ]
    for row in rows:
        parts.append(
            f"- task_gid: {row['task_gid']}  (similarity {row['score']:.2f})\n"
            f"  title: {row.get('title') or ''}\n"
            f"  notes: {(row.get('notes') or '')[:NOTES_CAP]}\n"
            f"  due_on: {row.get('due_on') or 'none'}"
        )
    return "\n".join(parts)


def _confirm(event: EmailClassifiedEvent, rows: list[dict]) -> Match:
    raw = claude.classify(
        system=SYSTEM_PROMPT, user=build_user_message(event, rows), schema=OUTPUT_SCHEMA
    )
    data = json.loads(raw)
    reason = str(data.get("reason") or "").strip()
    gid = data.get("task_gid") or None

    # The model may only choose from what it was shown. A gid outside the
    # candidate set is a hallucination, and acting on it would comment on a
    # task nobody compared against this email.
    if gid is not None and str(gid) not in {r["task_gid"] for r in rows}:
        logger.warning("relate confirm named a gid outside the candidates: %s", gid)
        gid = None
    if gid is not None and not asana.task_exists(str(gid)):
        gid = None
    if gid is None:
        return Match(reason=reason or "no candidate matched")
    return Match(
        task_gid=str(gid),
        resolves=bool(data.get("resolves")),
        reason=reason,
        evidence=[{"kind": "task", "ref": str(gid), "note": reason}],
    )


def match(event: EmailClassifiedEvent) -> Match:
    """Never raises. Any failure — Vertex, Postgres, Haiku, Asana — returns
    Match(), so the email is recorded as a plain suppression and no comment is
    posted. Worst-case outage behaviour is today's behaviour."""
    message_id = event.get("message_id", "")
    try:
        rows = _candidates(event)
    except Exception:  # noqa: BLE001 — no-match is the degradation
        logger.exception("relate candidate lookup failed message_id=%s", message_id)
        rows = []

    if not rows or rows[0]["score"] < SIMILARITY_FLOOR:
        best = rows[0]["score"] if rows else None
        logger.info("relate below floor best=%s message_id=%s", best, message_id)
        otel.tasks_related.add(1, {"matched": "false"})
        return Match(reason="no open task above the similarity floor")

    try:
        result = _confirm(event, rows)
    except Exception:  # noqa: BLE001
        logger.exception("relate confirm failed message_id=%s", message_id)
        otel.tasks_related.add(1, {"matched": "false"})
        return Match(reason="relate confirm unavailable")

    otel.tasks_related.add(1, {"matched": "true" if result.task_gid else "false"})
    logger.info(
        "relate matched=%s resolves=%s message_id=%s reason=%s",
        result.task_gid,
        result.resolves,
        message_id,
        result.reason,
    )
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_relating.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add services/relating.py tests/test_relating.py
git commit -m "feat: relating stage — nearest-neighbour, floor, confirm, verify"
```

---

### Task 7: Wire the three-way verdict into the handler

**Files:**
- Modify: `handlers/task_create.py:91-180`
- Test: `tests/test_task_create.py`

**Interfaces:**
- Consumes: `screening.screen`, `relating.match`, `models.events.Screening` / `Match`
- Produces: `task_create.handle` branches three ways, records `source="screen"` / `source="relate"`, and threads `verdict.priority` into the deadline gate, the title, the otel labels, and the `tasks` row

- [ ] **Step 1: Write the failing test**

Add to `tests/test_task_create.py`. First an autouse fixture beside `_default_triage`:

```python
@pytest.fixture(autouse=True)
def _default_screening(monkeypatch):
    from models.events import Screening
    from services import screening

    monkeypatch.setattr(screening, "screen", lambda event, **kw: Screening(priority="P1"))
```

Then helpers and tests:

```python
def _screen_as(monkeypatch, **kwargs):
    from models.events import Screening
    from services import screening

    verdict = Screening(**kwargs)
    monkeypatch.setattr(screening, "screen", lambda event, **kw: verdict)
    return verdict


def _relate_as(monkeypatch, **kwargs):
    from models.events import Match
    from services import relating

    result = Match(**kwargs)
    monkeypatch.setattr(relating, "match", lambda event: result)
    return result


def test_drop_verdict_suppresses_and_records(monkeypatch):
    _screen_as(monkeypatch, verdict="drop", reason="marketing newsletter", outcome="drop")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(category="ignore"))

    assert created == {}
    assert rows[0]["source"] == "screen"
    assert rows[0]["reason"] == "marketing newsletter"
    assert rows[0]["related_task_gid"] is None


def test_relate_verdict_comments_on_the_matched_task(monkeypatch):
    """The Enterprise case: a confirmation settles an open task."""
    _screen_as(monkeypatch, verdict="relate", reason="rental confirmation", outcome="relate")
    _relate_as(
        monkeypatch,
        task_gid="1217730397662201",
        resolves=True,
        reason="same reservation number",
        evidence=[{"kind": "task", "ref": "1217730397662201", "note": "same reservation"}],
    )
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    stories = []
    monkeypatch.setattr(asana, "create_story", lambda gid, text: stories.append((gid, text)))
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(category="reference"))

    assert created == {}
    assert stories[0][0] == "1217730397662201"
    assert "Looks resolved" in stories[0][1]
    assert rows[0]["source"] == "relate"
    assert rows[0]["related_task_gid"] == "1217730397662201"


def test_relate_with_no_match_still_records(monkeypatch):
    _screen_as(monkeypatch, verdict="relate", reason="delivery notice", outcome="relate")
    _relate_as(monkeypatch, reason="no open task above the similarity floor")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    stories = []
    monkeypatch.setattr(asana, "create_story", lambda gid, text: stories.append(gid))

    task_create.handle(make_email_event(category="ignore"))

    assert stories == []
    assert rows[0]["source"] == "relate"
    assert rows[0]["related_task_gid"] is None
    assert rows[0]["reason"] == "no open task above the similarity floor"


def test_screener_rescues_an_ignore_email(monkeypatch):
    """The Dana case: inbox said ignore/P3, the screener says task/P1."""
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    _screen_as(monkeypatch, verdict="task", priority="P1", reason="bank statements attached")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    inserts = _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch, due="2026-09-01", title="review Dana's bank statements")
    created = _capture_create(monkeypatch)
    placed = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda gid, sec: placed.append(sec))

    task_create.handle(make_email_event(category="ignore", importance="P3"))

    assert created["title"] == "[P1] review Dana's bank statements"
    assert created["due_date"] == "2026-09-01"       # P1 → deadline extraction ran
    assert inserts[0]["importance"] == "P1"          # screener priority, not inbox's P3
    assert placed == ["sec-review"]                  # rescued → Review, not unsectioned


def test_deadline_extraction_follows_screener_priority(monkeypatch):
    _screen_as(monkeypatch, priority="P3")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _, deadline_calls = _stub_enrichment(monkeypatch, due="2026-09-01", title="do a thing")
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(importance="P0"))

    assert deadline_calls == []          # inbox said P0; the screener says P3
    assert created["due_date"] is None


def test_title_falls_back_to_subject_with_screener_priority(monkeypatch):
    _screen_as(monkeypatch, priority="P2")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch, title=None)
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(subject="Quarterly report"))

    assert created["title"] == "[P2] Quarterly report"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_task_create.py -q`
Expected: FAIL — `handle` still calls `policy.warrants_task` and knows nothing about `relate`

- [ ] **Step 3: Write minimal implementation**

In `handlers/task_create.py`, add `relating` and `screening` to the `services` import list and `Screening` to the `models.events` import.

Replace the gate-1 block at the top of `handle` (lines 91-96):

```python
def handle(event: EmailClassifiedEvent) -> None:
    verdict: Screening = screening.screen(event)

    if verdict.verdict == "drop":
        _suppress(
            event,
            reason=verdict.reason,
            source="screen",
            related_task_gid=None,
            evidence=[],
        )
        return

    if verdict.verdict == "relate":
        # Needs no work of its own, but may report on an open task. relating
        # finds it or gives up; either way the email is recorded, and a match
        # reaches _suppress()'s comment branch.
        found = relating.match(event)
        _suppress(
            event,
            reason=found.reason or verdict.reason,
            source="relate",
            related_task_gid=found.task_gid,
            evidence=found.evidence,
            resolves=found.resolves,
        )
        return

    decision: Decision = triage.decide(event, screening=verdict)
```

Replace the deadline gate (line 120):

```python
    if verdict.priority in ("P0", "P1"):
```

Replace the title line (line 134):

```python
    # The authoritative [PX] prefix per the "Title" section of
    # docs/task-content-standard.md (doc wins over code). email_summary
    # produces a clean "{verb} {object}"; the subject is the last resort.
    title = f"[{verdict.priority}] {summary.title or event['subject'] or '(no subject)'}"
```

Replace the otel line (line 148) — inbox's importance stays as a second label so the two classifiers' agreement remains measurable:

```python
    otel.tasks_created.add(
        1,
        {
            "category": event["category"],
            "importance": verdict.priority,
            "inbox_importance": event["importance"],
        },
    )
```

Replace the `repo_tasks.insert` importance argument (line 157):

```python
                importance=verdict.priority,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_task_create.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add handlers/task_create.py tests/test_task_create.py
git commit -m "feat: three-way gate 1 in the handler; relate emails become comments"
```

---

### Task 8: Gate 2 — delete the urgent bypass, consume the verdict

**Reversal warning.** The unamended spec kept the urgent bypass keyed on `category == "urgent"`. The amendment **deletes it**: it is the heaviest remaining coupling to inbox, on the one label the correction log disputes most (9 corrections away from `urgent`, 0 toward it). `triage.decide` is fail-open by contract, so running gate 2 on urgent mail cannot swallow it through failure — only through an affirmative, reasoned suppression, which is the exposure every other task-bound email already carries.

An existing test asserts the bypass. It is **deleted**, not adapted.

**Files:**
- Modify: `services/triage.py` (`build_user_message`, `decide`, `_gid_exists` → `asana.task_exists`)
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `models.events.Screening`, `clients.asana.task_exists`
- Produces: `triage.decide(event, *, today: str | None = None, screening: Screening | None = None)`; `triage.build_user_message(event, *, today, roles, screening=None)`

- [ ] **Step 1: Delete the bypass test and write the replacements**

Delete `tests/test_triage.py::test_urgent_short_circuits_without_calling_agent` (lines 146-150) entirely.

Append:

```python
def test_urgent_now_runs_gate_two(monkeypatch):
    """The bypass is gone: urgent mail is triaged like everything else."""
    from models.events import Screening

    called = []
    monkeypatch.setattr(
        claude, "run_agent", lambda **kw: called.append(1) or (_ok(actionable=True), "end_turn")
    )
    _roles(monkeypatch, "")
    _gid_verifies(monkeypatch, True)

    result = triage.decide(
        make_email_event(category="urgent"), screening=Screening(priority="P0")
    )

    assert called == [1]
    assert result.actionable is True


def test_user_message_carries_the_screening_verdict():
    from models.events import Screening

    msg = triage.build_user_message(
        make_email_event(),
        today="2026-08-24",
        roles="",
        screening=Screening(verdict="task", priority="P1", reason="statements attached"),
    )
    assert "Screened: task / P1 — statements attached" in msg
    assert "Classified:" not in msg


def test_user_message_without_a_screening_verdict():
    msg = triage.build_user_message(make_email_event(), today="2026-08-24", roles="")
    assert "Screened:" not in msg


def test_decide_forwards_the_screening_verdict(monkeypatch):
    from models.events import Screening

    captured = {}
    _agent(monkeypatch, _ok(actionable=True), capture=captured)
    _roles(monkeypatch, "")
    _gid_verifies(monkeypatch, True)
    triage.decide(
        make_email_event(category="review"),
        screening=Screening(priority="P0", reason="signed form requested"),
    )
    assert "Screened: task / P0 — signed form requested" in captured["user"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_triage.py -q`
Expected: FAIL — `test_urgent_now_runs_gate_two` asserts `called == [1]` but the bypass short-circuits, and `build_user_message()` rejects `screening=`

- [ ] **Step 3: Write minimal implementation**

In `services/triage.py`, add `Screening` to the `models.events` import.

Change the `build_user_message` signature:

```python
def build_user_message(
    event: EmailClassifiedEvent,
    *,
    today: str,
    roles: str,
    screening: Screening | None = None,
) -> str:
```

Then replace the whole second `parts.append(...)` block — the one starting `"The email:\n"`. The only change is the `Classified:` line becoming a conditional `Screened:` line; everything else is byte-identical:

```python
    parts.append(
        "The email:\n"
        f"Subject: {event.get('subject') or ''}\n"
        f"From: {event.get('sender') or ''}"
        + (f" ({event['sender_display']})" if event.get("sender_display") else "")
        + "\n"
        f"To: {', '.join(event.get('to') or [])}\n"
        f"Cc: {', '.join(event.get('cc') or [])}\n"
        f"Received: {event.get('received_at') or ''}\n"
        + (
            f"Screened: {screening.verdict} / {screening.priority} — {screening.reason}\n"
            if screening
            else ""
        )
        + f"Message id: {event.get('message_id')}\n\n"
        f"{(event.get('body') or '')[:BODY_CAP]}"
    )
```

Replace `_gid_exists` with a delegation to the shared helper from Task 5 — keep the name so `_parse`'s injectable default is untouched:

```python
def _gid_exists(task_gid: str) -> bool:
    """The spec's fail-open case: a related_task_gid we cannot fetch is
    treated as no match. Shared with services/relating.py."""
    return asana.task_exists(task_gid)
```

Change `decide` — **delete the urgent bypass** and thread the verdict:

```python
def decide(
    event: EmailClassifiedEvent,
    *,
    today: str | None = None,
    screening: Screening | None = None,
) -> Decision:
    """Gate 2. Never raises; every failure returns the actionable default.

    There is no category bypass. `urgent` used to skip this gate entirely —
    the heaviest coupling to inbox in the repo, on the label the correction
    log disputes most. Being fail-open, running here cannot swallow urgent
    mail through failure; only an affirmative, reasoned suppression can, which
    is the exposure every other task-bound email already carries."""
    message_id = event["message_id"]
    today = today or date.today().isoformat()
    roles = standing_context.section("Roles")
    user = build_user_message(event, today=today, roles=roles, screening=screening)
```

(The rest of `decide` is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_triage.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/triage.py tests/test_triage.py
git commit -m "feat: delete the urgent bypass; gate 2 reads the screener verdict"
```

---

### Task 9: Action-item `source` must not forge a confirmation

`services/task_content.py:110` hardcodes `human_confirmation` on the confirm button. For an email the screener rescues from `reference`/`ignore`, the `else` branch renders **"Confirmed review"** — but inbox never said `review`. Clicking it would record a human *confirmation* of a classification that was never made, injecting a false label into the only labelled signal the pipeline has — and into the very corpus the dry run partitions on.

Only reachable once the screener can rescue mail, which is why it lands here.

**Files:**
- Modify: `services/task_content.py:100-114`
- Test: `tests/test_task_content.py`

**Interfaces:**
- Consumes: nothing
- Produces: the confirm action item's `source` is derived, not hardcoded

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_content.py`:

```python
def test_rescued_email_records_a_correction_not_a_confirmation(monkeypatch):
    """A screener-rescued ignore email must not forge a human_confirmation."""
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    monkeypatch.delenv("WEBHOOK_LABEL_TOKEN", raising=False)
    content = task_content.for_email(make_email_event(category="ignore"), [], [])
    confirm_label, confirm_url = content.action_items[0]
    assert confirm_label == "Confirmed review"
    assert "label=review" in confirm_url
    assert "source=human_correction" in confirm_url


def test_matching_category_still_records_a_confirmation(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    monkeypatch.delenv("WEBHOOK_LABEL_TOKEN", raising=False)
    content = task_content.for_email(make_email_event(category="review"), [], [])
    assert "source=human_confirmation" in content.action_items[0][1]

    respond = task_content.for_email(make_email_event(category="respond"), [], [])
    assert "source=human_confirmation" in respond.action_items[0][1]
```

Confirm the imports at the top of `tests/test_task_content.py` include `task_content` and `make_email_event`; add them if absent.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: FAIL — `assert 'source=human_correction' in '...source=human_confirmation...'`

- [ ] **Step 3: Write minimal implementation**

In `services/task_content.py`, replace the block from `if event["category"] == "respond":` through the `action_items` list:

```python
    if event["category"] == "respond":
        confirm_label, confirm_text = "respond", "Confirmed respond"
        alt_label, alt_text = "review", "Review instead"
    else:
        confirm_label, confirm_text = "review", "Confirmed review"
        alt_label, alt_text = "respond", "Respond instead"

    # These buttons write back to inbox's classifier through the label
    # webhook, so they must speak inbox's vocabulary — this is the correction
    # channel, not a leaked dependency. But the source has to be DERIVED: for
    # an email the gate-1 screener rescued from reference/ignore, the confirm
    # button offers a label inbox never assigned, and recording that as a
    # human_confirmation would forge a confirmation of a classification that
    # was never made — corrupting the one labelled signal the pipeline has.
    confirm_source = (
        "human_confirmation" if confirm_label == event["category"] else "human_correction"
    )

    action_items: list[tuple[str, str]] = [
        (confirm_text, _action_url(message_id, confirm_label, confirm_source)),
        (alt_text, _action_url(message_id, alt_label, "human_correction")),
        ("Reference", _action_url(message_id, "reference", "human_correction")),
        ("Ignore", _action_url(message_id, "ignore", "human_correction")),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: PASS, including the pre-existing `test_for_email_review_action_items_and_source` (a `review` email still yields `human_confirmation`).

- [ ] **Step 5: Commit**

```bash
git add services/task_content.py tests/test_task_content.py
git commit -m "fix: derive the action-item source so a rescued email records a correction"
```

---

### Task 10: The dry-run backtest harness

The whole point of the spec's verification section. Local-only — it reads the **inbox** database, which the deployed tasks service has no access to, *and* this repo's own DB for the embedding corpus.

`relate` and `task` must be scored **separately**. A readout of "promoted 180/1120" is meaningless if 170 of those are `relate`.

**Files:**
- Create: `scripts/backtest_screening.py`
- Modify: `README.md`, `.gitignore`

**Interfaces:**
- Consumes: `services.screening.screen`, `services.relating.match` / `_candidates`
- Produces: a TSV at `--out` with columns `corpus, message_id, expected, verdict, priority, outcome, matched_gid, resolves, best_score, neighbours, sender, subject, reason`, plus a summary on stdout

- [ ] **Step 1: Write the corpus queries and the row → event adapter**

Create `scripts/backtest_screening.py`:

```python
#!/usr/bin/env python
"""Dry run of gate 1 over historical mail. Creates nothing, comments nothing.

Reads the INBOX database (not this service's) for the corpus and replays every
email through services/screening.screen, then services/relating.match for the
`relate` verdicts, writing a TSV for analysis. relating.match only reads — the
comment is posted by handlers/task_create.py::_suppress, which is never called
here.

Local-only: the deployed tasks service has no inbox DB access.

Why a dry run and not a shadow period: the inbox DB holds zero recorded
misses. Every human correction runs the other way (Ben downgrades false
positives; mail filed `ignore` is archived and never seen again). But the
baseline on that pile is zero promotions, so the screener cannot be worse than
today on recall — only on precision, which is exactly what this measures
without an answer key.

Usage:
    .venv/bin/python scripts/backtest_screening.py --out backtest.tsv
    .venv/bin/python scripts/backtest_screening.py --corpus recall_probe --limit 40
    .venv/bin/python scripts/backtest_screening.py --no-relate   # screener only, cheaper

Needs BOTH databases: this repo's .env (tasks DB for the embedding corpus,
ANTHROPIC_API_KEY, INBOX_API_*) plus ~/src/inbox/.env for the corpus itself.
Run scripts/fetch-env.sh first.
"""

import argparse
import collections
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_LLM_CTE = """
WITH llm AS (
  SELECT DISTINCT ON (message_id) message_id, category, importance
  FROM classifications WHERE source='llm'
  ORDER BY message_id, created_at DESC),
hum AS (
  SELECT DISTINCT ON (message_id) message_id, category
  FROM classifications WHERE source='human_correction'
  ORDER BY message_id, created_at DESC)
"""

_SELECT = """
SELECT m.id, m.external_id, m.sender, m.sender_display, m.subject,
       m.body, m.received_at, llm.category, llm.importance
"""

# expected: the verdict a correct screener reaches. "non_task" accepts either
# drop or relate — both are non-task outcomes and neither floods the list.
CORPORA: dict[str, tuple[str, str | None]] = {
    "negative": (
        _LLM_CTE + _SELECT + """
        FROM llm JOIN messages m ON m.id = llm.message_id
        WHERE llm.category IN ('ignore','reference')
          AND llm.message_id NOT IN (SELECT message_id FROM hum)
        ORDER BY m.received_at DESC""",
        "non_task",
    ),
    # Ben explicitly corrected these DOWN to ignore/reference. Sharpest rows in
    # the corpus — he actively ruled "not a task" on each. A hard negative
    # landing on `relate` is acceptable: he ruled "not a task", not
    # "not relevant".
    "hard_negative": (
        _LLM_CTE + _SELECT + """
        FROM hum JOIN llm USING (message_id) JOIN messages m ON m.id = hum.message_id
        WHERE hum.category IN ('ignore','reference')
        ORDER BY m.received_at DESC""",
        "non_task",
    ),
    "regression_reclass": (
        _LLM_CTE + _SELECT + """
        FROM hum JOIN llm USING (message_id) JOIN messages m ON m.id = hum.message_id
        WHERE hum.category IN ('urgent','review','respond')
        ORDER BY m.received_at DESC""",
        "task",
    ),
    # Dana's seam: near-empty bodies where the signal is elsewhere.
    "recall_probe": (
        _LLM_CTE + _SELECT + """
        FROM llm JOIN messages m ON m.id = llm.message_id
        WHERE llm.category IN ('ignore','reference')
          AND length(coalesce(m.body,'')) < 200
        ORDER BY m.received_at DESC""",
        None,
    ),
}


def load_env(path: pathlib.Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def connect(env: dict[str, str]):
    from google.cloud.sql.connector import Connector

    return Connector().connect(
        env["CLOUD_SQL_CONNECTION_NAME"],
        "pg8000",
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        db=env.get("POSTGRES_DB", "inbox"),
    )


def to_event(row: tuple) -> dict:
    """inbox `messages` row → the EmailClassifiedEvent shape screen() reads.

    has_attachments is set True unconditionally: messages.raw stores the Graph
    *webhook notification*, not the message, so the DB cannot answer it.
    attachment_lines() then asks inbox-api, which answers authoritatively and
    returns [] when there are none."""
    mid, graph_id, sender, sender_display, subject, body, received_at, category, importance = row
    return {
        "event": "email_classified",
        "message_id": str(mid),
        "graph_message_id": graph_id,
        "has_attachments": True,
        "category": category,
        "importance": importance or "P3",
        "confidence": 0.0,
        "subject": subject or "",
        "sender": sender or "",
        "sender_display": sender_display or sender or "",
        "to": [],
        "cc": [],
        "received_at": str(received_at),
        "tags": [],
        "reasoning": "",
        "body": body or "",
        "body_html": None,
        "web_link": None,
    }
```

- [ ] **Step 2: Write the runner and the split summary**

Append to `scripts/backtest_screening.py`:

```python
def _neighbours(event) -> tuple[float, str]:
    """Top-3 neighbours and the best cosine score, so the similarity floor can
    be tuned from the same run that tunes the prompt. Embeddings are the
    expensive part and they cache."""
    from services import relating

    try:
        rows = relating._candidates(event)
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        return 0.0, f"error: {exc}"
    if not rows:
        return 0.0, ""
    summary = " | ".join(f"{r['task_gid']}:{r['score']:.2f}:{(r.get('title') or '')[:40]}"
                         for r in rows)
    return rows[0]["score"], summary


def run(args) -> int:
    from services import relating, screening

    env = load_env(pathlib.Path(args.inbox_env).expanduser())
    conn = connect(env)
    cur = conn.cursor()

    rows_out: list[dict] = []
    skipped = 0
    for name in args.corpus or list(CORPORA):
        sql, expected = CORPORA[name]
        if args.limit:
            sql += f" LIMIT {int(args.limit)}"
        cur.execute(sql)
        for row in cur.fetchall():
            event = to_event(row)
            verdict = screening.screen(event)
            if verdict.outcome == "fail_open":
                skipped += 1
                print(f"  fail_open (not scored): {event['message_id']}", file=sys.stderr)
                continue

            best_score, neighbours, matched_gid, resolves = 0.0, "", "", ""
            if verdict.verdict == "relate" and not args.no_relate:
                best_score, neighbours = _neighbours(event)
                found = relating.match(event)
                matched_gid = found.task_gid or ""
                resolves = str(found.resolves)

            rows_out.append(
                {
                    "corpus": name,
                    "message_id": event["message_id"],
                    "expected": expected or "",
                    "verdict": verdict.verdict,
                    "priority": verdict.priority,
                    "outcome": verdict.outcome,
                    "matched_gid": matched_gid,
                    "resolves": resolves,
                    "best_score": f"{best_score:.3f}" if best_score else "",
                    "neighbours": neighbours,
                    "sender": event["sender"],
                    "subject": event["subject"][:80],
                    "reason": verdict.reason,
                }
            )
    conn.close()

    with open(args.out, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows_out[0]), delimiter="\t")
        writer.writeheader()
        writer.writerows(rows_out)

    summarise(rows_out, skipped, args.out)
    return 0


def _wrong(row: dict) -> bool:
    """A scoring miss. `non_task` accepts drop OR relate."""
    if not row["expected"]:
        return False
    if row["expected"] == "non_task":
        return row["verdict"] == "task"
    return row["verdict"] != row["expected"]


def summarise(rows: list[dict], skipped: int, out: str) -> None:
    print(f"\nwrote {len(rows)} rows to {out} ({skipped} fail_open, not scored)\n")
    print(f"{'corpus':22} {'n':>5} {'task':>6} {'relate':>7} {'drop':>6} "
          f"{'task rate':>10}  misses")
    for name in dict.fromkeys(r["corpus"] for r in rows):
        group = [r for r in rows if r["corpus"] == name]
        counts = collections.Counter(r["verdict"] for r in group)
        rate = counts["task"] / len(group) if group else 0.0
        print(f"{name:22} {len(group):5} {counts['task']:6} {counts['relate']:7} "
              f"{counts['drop']:6} {rate:9.1%}  {sum(1 for r in group if _wrong(r))}")

    relates = [r for r in rows if r["verdict"] == "relate" and r["best_score"]]
    if relates:
        matched = [r for r in relates if r["matched_gid"]]
        print(f"\nrelate: {len(matched)}/{len(relates)} matched an open task "
              f"({len(matched) / len(relates):.0%}); "
              f"{sum(1 for r in matched if r['resolves'] == 'True')} claim resolves")
        print("  a climbing no-match rate means the floor or the prompt is wrong")

    domains = collections.Counter(
        r["sender"].rsplit("@", 1)[-1]
        for r in rows
        if r["corpus"] == "negative" and r["verdict"] == "task"
    )
    if domains:
        print("\ntop `task`-promoted sender domains in `negative` "
              "(a pile of one vendor means the prompt is too loose):")
        for domain, count in domains.most_common(12):
            print(f"  {count:4}  {domain}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="backtest.tsv")
    parser.add_argument("--corpus", action="append", choices=list(CORPORA))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-relate", action="store_true",
                        help="skip the relating stage (screener verdicts only, cheaper)")
    parser.add_argument("--inbox-env", default="~/src/inbox/.env")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Smoke it on a small slice**

```bash
cd ~/src/tasks
set -a; source .env; set +a
.venv/bin/python scripts/backtest_screening.py --corpus recall_probe --limit 5 --out /tmp/smoke.tsv
```

Expected: 5 rows, a summary table, no traceback. Verify `reason` is populated — an empty column means the model is not filling the schema.

- [ ] **Step 4: Run the full dry run**

```bash
.venv/bin/python scripts/backtest_screening.py --out backtest.tsv
```

Expected: ~1,200 rows across the four corpora, roughly 20-35 min, ~$3.

Read the summary against the spec's ship gate:

- **`task` promotion rate** on `negative` reviewed and accepted — `relate` is excluded from this number and reported separately
- `hard_negative` **`task`** promotions ≤ 2 of 24 (a `relate` there is acceptable)
- `regression_reclass` 6/6 on `task`
- Dana's message (`0053c9c2-e6c3-4ca7-ac46-3d6d0fc67553`) → `task`, priority above P3 — grep `recall_probe`
- Enterprise (`6eb38d8b-18f6-48c8-a4f1-26be68256681`) → `relate`, matched to `1217730397662201`, `resolves=True`
- **Zero wrong-task matches** on a hand-checked sample of rows with a `matched_gid`. This is a hard zero, not a rate — a comment on the wrong task has no cheap recovery. Sort by `best_score` ascending and read the weakest matches first; that is where a floor set too low shows up.

If the `task` rate is high and the domain histogram is dominated by one vendor, tune `SYSTEM_PROMPT` in `services/screening.py`. If wrong matches appear, raise `SIMILARITY_FLOOR` in `services/relating.py`. Re-run. That loop is why this script exists.

- [ ] **Step 5: Record the urgent-bypass denominators**

The spec's narrow call 2 cites 9 corrections away from `urgent` and 0 toward it, and asks for the *rate*. Compute it and paste it into the spec:

```sql
WITH llm AS (SELECT DISTINCT ON (message_id) message_id, category FROM classifications
             WHERE source='llm' ORDER BY message_id, created_at DESC)
SELECT count(*) FROM llm WHERE category='urgent';
```

The rate is 9 / that count.

- [ ] **Step 6: Add the README section and gitignore the output**

Add under the local-dev section of `README.md`:

```markdown
### Backtesting the screener

`scripts/backtest_screening.py` replays gate 1 over historical mail from the
**inbox** database and creates nothing. Use it after any change to
`services/screening.py::SYSTEM_PROMPT` or `services/relating.py::SIMILARITY_FLOOR`.

    set -a; source .env; set +a
    .venv/bin/python scripts/backtest_screening.py --out backtest.tsv

`task` and `relate` are scored separately — a promotion rate that folds them
together is meaningless. Corpora and the ship gate:
`docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md` §Verification.
Full pass is ~1,200 screener calls plus the relate stage (~$3, 20-35 min);
`--no-relate` for a cheaper screener-only pass, `--corpus recall_probe --limit 20`
for a quick check.
```

Add to `.gitignore` if not already covered:

```
backtest*.tsv
```

The output holds real subjects and senders, and this repo is public.

- [ ] **Step 7: Commit**

```bash
git add scripts/backtest_screening.py README.md .gitignore
git commit -m "feat: dry-run backtest harness scoring task and relate separately"
```

---

### Task 11: Documentation

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md` (status, measured numbers)

- [ ] **Step 1: Update the Task policy section of `CLAUDE.md`**

Replace the sentence beginning `services/policy.py::warrants_task — urgent/review/respond → task (gate 1).` with:

```markdown
`services/screening.py::screen` — gate 1. A Haiku call over **every** email
inbox publishes, whatever category it was filed under. It reads the email, its
attachment metadata (names/types/sizes, fetched via `graph_message_id` — NOT
`message_id`, which is inbox's UUID and gets rejected), and the `Roles` section
of the declared facts, and returns a three-way verdict:

- `task` → gate 2 (`services/triage.py`), then enrichment and creation.
- `relate` → `services/relating.py`: embed the email, take the nearest open
  tasks from `task_index`, apply a similarity floor, confirm with one Haiku
  call, verify the gid against Asana. A match becomes a **comment** on that
  task via `_suppress()`'s related-task branch; no match is a normal outcome
  and still records a row. Nothing here ever closes a task.
- `drop` → a `suppressed_emails` row.

Tasks owns its own priority from here: the `[PX]` prefix and the P0/P1 deadline
gate read the screener's verdict, not inbox's `importance`. Section placement
still reads inbox's `category` (a mailbox-routing fact), defaulting to Review
for a rescued email. `services/policy.py::warrants_task` is retained as the
OUTAGE fallback — a Claude failure degrades gate 1 to the old category rule
(`task`/`drop` only, never `relate`) rather than flooding the list.

**There is no urgent bypass.** `urgent` mail runs gate 2 like everything else.
```

- [ ] **Step 2: Update the Local dev section of `CLAUDE.md`**

Add to the code block:

```bash
.venv/bin/python scripts/backtest_screening.py --out backtest.tsv  # dry-run gate 1 over history
```

- [ ] **Step 3: Fill in the spec's measured numbers**

In the spec: change `**Status:** design approved, not yet implemented` to `**Status:** implemented`; record the dry-run results under §Verification → Ship gate; and replace the "compute them from the inbox DB during the dry run and record them here" placeholder in narrow call 2 with the rate from Task 10 Step 5.

- [ ] **Step 4: Verify the whole suite and lint**

```bash
.venv/bin/pytest tests/ -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Expected: all pass.

- [ ] **Step 5: Commit and open the PR**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md
git commit -m "docs: record tasks-owned screening and the relate path in CLAUDE.md"
```

Then use the `/pr-open` skill. Include the dry-run summary table in the PR body — it is the evidence for the change.

---

## Deferred — file as issues, do not implement here

1. **`triage.py`'s `get_email` id bug.** `search_emails(mode="db")` returns inbox UUIDs; feeding one to `get_email` yields `ErrorInvalidIdMalformed` → 502. The agent has been silently losing evidence on that path. Confirmed by hand 2026-08-27 against `message_id=0053c9c2-e6c3-4ca7-ac46-3d6d0fc67553`.
2. **`get_attachments` returns `content_bytes`.** The screener drops it immediately, but a 65 KB CSV still crosses the wire as ~88 KB of base64 on every attachment-bearing email — and 1,200× over a full backtest. inbox-api wants a `?metadata_only=true` parameter.
3. **inbox's classifier is still blind to attachment names.** `~/src/inbox/clients/azure/email.py:82::get_attachment_names()` is dead code. Wiring it into `services/classification.py::build_prompt` would improve `category` for every consumer, not just tasks.
4. **A bypass keyed on the screener's own `priority == "P0"`**, if `tasks_suppressed{category="urgent"}` ever shows gate 2 wrongly suppressing urgent mail. One line; do not pre-ship it.

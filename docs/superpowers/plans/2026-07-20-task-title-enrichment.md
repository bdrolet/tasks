# Task Title Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the pipeline's `[PX] {subject}` task title with a context-driven `[PX] {verb} {object}` action title generated inside the existing `email_summary` Haiku call, with a deterministic fallback to the old format.

**Architecture:** The Haiku call in `services/email_summary.py` already reads each qualifying email and returns JSON; we widen its output to include a normalized `title` string (verb + object, no priority tag). `handlers/task_create.py` prepends `[PX]` and passes the result to `clients/asana.py::create_task`, which uses it as the Asana `name` or falls back to `[{importance}] {subject}` when absent. Title enrichment is best-effort — a failed or empty model response never blocks task creation.

**Tech Stack:** Python 3.13, pytest, Anthropic Claude Haiku (via `clients/claude.py`), Asana REST.

## Global Constraints

- **Layer rules:** `clients/` = I/O only (no title *logic*, only the last-resort default string); `services/` = business logic incl. the title prompt/guidance; `handlers/` orchestrate. Prompt text lives in `services/`, never in `clients/claude.py`.
- **Best-effort enrichment:** a Haiku failure or missing/garbled `title` must never crash the event or block task creation — fall back to `[{importance}] {subject}`.
- **`[PX]` applied in code**, never by the model: the model emits only `{verb} {object}`; the handler prepends the priority token so a bad response can't corrupt it.
- **Title standard:** imperative verb first, then 2–5 word object; sentence case; no trailing punctuation; ≤ ~60 chars / ~8 words after the prefix.
- **Canonical standard doc:** `docs/superpowers/specs/2026-07-20-task-title-enrichment-design.md`. Other locations link to it, not duplicate it.
- Run tests with `.venv/bin/pytest`.

---

### Task 1: Generate and normalize the title in `email_summary`

**Files:**
- Modify: `models/events.py:43-46` (widen `EmailSummary`)
- Modify: `services/email_summary.py:66-88` (prompt + parse + normalize; add module docstring)
- Test: `tests/test_email_summary.py`

**Interfaces:**
- Produces: `EmailSummary.title: str | None` — normalized `{verb} {object}` (no `[PX]` prefix), or `None` when the model omitted it or it normalized to empty.
- Produces: `email_summary._normalize_title(raw: str | None) -> str | None` — strips whitespace/trailing punctuation, collapses inner whitespace, truncates on a word boundary to `MAX_TITLE_CHARS`, returns `None` for empty.
- Consumes: existing `clients.claude.summarize(prompt) -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_email_summary.py`:

```python
def test_generate_parses_title(monkeypatch):
    monkeypatch.setattr(
        claude,
        "summarize",
        lambda prompt: '{"key_points": ["A"], "title": "Review Q3 board deck"}',
    )
    assert email_summary.generate(make_email_event()).title == "Review Q3 board deck"


def test_generate_title_missing_is_none(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda prompt: '{"key_points": ["A"]}')
    assert email_summary.generate(make_email_event()).title is None


def test_normalize_title_strips_punctuation_and_whitespace():
    assert email_summary._normalize_title("  Review  the   deck.  ") == "Review the deck"


def test_normalize_title_empty_is_none():
    assert email_summary._normalize_title("   ") is None
    assert email_summary._normalize_title(None) is None


def test_normalize_title_truncates_on_word_boundary():
    long = "Review the extremely detailed quarterly board deck before the meeting happens soon"
    out = email_summary._normalize_title(long)
    assert len(out) <= email_summary.MAX_TITLE_CHARS
    assert not out.endswith(" ")
    assert long.startswith(out)  # truncated, not reworded
```

Also update the existing failure test to assert `title` is `None`:

```python
def test_generate_survives_claude_failure(monkeypatch):
    def boom(prompt):
        raise RuntimeError("api down")

    monkeypatch.setattr(claude, "summarize", boom)
    summary = email_summary.generate(make_email_event())
    assert summary.key_points == []
    assert summary.title is None
    assert summary.relevant_links == [["https://docs.example/q2", "the Q2 report"]]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_email_summary.py -q`
Expected: FAIL — `AttributeError`/`TypeError` on `EmailSummary.title` and `email_summary._normalize_title` not defined.

- [ ] **Step 3: Widen the `EmailSummary` dataclass**

In `models/events.py`, change:

```python
@dataclass
class EmailSummary:
    key_points: list[str] = field(default_factory=list)
    relevant_links: list[list[str]] = field(default_factory=list)  # [url, label]
```

to:

```python
@dataclass
class EmailSummary:
    key_points: list[str] = field(default_factory=list)
    relevant_links: list[list[str]] = field(default_factory=list)  # [url, label]
    title: str | None = None  # normalized "{verb} {object}", no [PX] prefix
```

- [ ] **Step 4: Add the normalizer and extend the prompt/parse in `email_summary.py`**

Add a module docstring at the very top of `services/email_summary.py` (above `import json`):

```python
"""Claude Haiku enrichment for a qualifying email: key points, relevant links,
and an actionable task title.

Title standard: "{verb} {object}" — imperative verb first, then a 2-5 word
object, sentence case, no trailing punctuation, <= ~60 chars. The [PX] priority
prefix is added by the caller (handlers/task_create.py), never the model.
Canonical: docs/superpowers/specs/2026-07-20-task-title-enrichment-design.md
"""
```

Add a constant and helper near the top (after the existing `_GENERIC_LABELS` definition, around line 16):

```python
MAX_TITLE_CHARS = 60


def _normalize_title(raw: str | None) -> str | None:
    """Clean a model-produced "{verb} {object}" title, or None if empty."""
    if not raw:
        return None
    title = " ".join(raw.split()).rstrip(".!,;:")
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip()
    return title or None
```

Replace the prompt and parsing in `generate()` (current lines 71-87). The new prompt requests `title`; parsing pulls it out and normalizes it:

```python
    prompt = (
        "Summarize this email in 2-3 concise bullet points, and write a task "
        "title for it. Be specific about what action is requested or what "
        "information is conveyed. No preamble.\n"
        "The title must be an actionable next step: start with an imperative "
        "verb, then 2-5 words naming what the action is on. Sentence case, no "
        "trailing punctuation, 8 words max. Do NOT include a priority tag. "
        'Examples: "Review Q3 board deck", "Reply to Sarah on contract redlines".\n'
        'Return JSON only: {"key_points": ["point 1", "point 2"], "title": "Verb object"}\n\n'
        f"Subject: {event['subject']}\n"
        f"From: {event['sender_display'] or event['sender']}\n\n"
        f"{body_text}"
    )
    key_points: list[str] = []
    title: str | None = None
    try:
        raw = claude.summarize(prompt)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        data = json.loads(raw)
        key_points = data.get("key_points", [])
        title = _normalize_title(data.get("title"))
    except Exception:
        logger.warning("Email summary generation failed for message_id=%s", event["message_id"])

    return EmailSummary(key_points=key_points, relevant_links=links, title=title)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_email_summary.py -q`
Expected: PASS (all, including the two pre-existing summary tests).

- [ ] **Step 6: Commit**

```bash
git add models/events.py services/email_summary.py tests/test_email_summary.py
git commit -m "feat: generate actionable task title in email_summary Haiku call"
```

---

### Task 2: Accept an enriched `title` in `create_task`

**Files:**
- Modify: `clients/asana.py:92-111` (`create_task` signature + `name`)
- Test: `tests/test_asana_client.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `create_task(event, *, tag_gids=None, due_date=None, html_notes="", title: str | None = None)` — when `title` is given it becomes the Asana `name` verbatim; when `None` it falls back to `[{importance}] {subject}`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_asana_client.py`:

```python
def test_create_task_uses_enriched_title(monkeypatch):
    calls = _capture(
        monkeypatch, _resp(201, {"data": {"gid": "42", "permalink_url": "https://a/42"}})
    )
    asana.create_task(make_email_event(), title="[P1] Review the quarterly report")
    assert calls[0]["json"]["data"]["name"] == "[P1] Review the quarterly report"
```

(The existing `test_create_task_builds_payload`, which passes no `title`, already asserts the fallback `name == "[P1] Quarterly report"` and must stay green.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_asana_client.py::test_create_task_uses_enriched_title -v`
Expected: FAIL — `create_task()` got an unexpected keyword argument `title`.

- [ ] **Step 3: Add the `title` parameter and use it**

In `clients/asana.py`, change the signature (line 92-98) to add the keyword:

```python
def create_task(
    event: EmailClassifiedEvent,
    *,
    tag_gids: list[str] | None = None,
    due_date: str | None = None,
    html_notes: str = "",
    title: str | None = None,
) -> CreatedTask | None:
```

and change the `name` line (line 107) from:

```python
        "name": f"[{event['importance']}] {event['subject'] or '(no subject)'}",
```

to:

```python
        "name": title or f"[{event['importance']}] {event['subject'] or '(no subject)'}",
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_asana_client.py -q`
Expected: PASS (new test plus existing `test_create_task_builds_payload` fallback).

- [ ] **Step 5: Commit**

```bash
git add clients/asana.py tests/test_asana_client.py
git commit -m "feat: create_task accepts an enriched title, falls back to [PX] subject"
```

---

### Task 3: Wire the enriched title through the handler

**Files:**
- Modify: `handlers/task_create.py:20-41`
- Test: `tests/test_task_create.py`

**Interfaces:**
- Consumes: `EmailSummary.title` (Task 1), `create_task(..., title=...)` (Task 2).
- Produces: the handler builds `f"[{importance}] {summary.title}"` when a title exists, else passes `title=None`.

- [ ] **Step 1: Write the failing test**

In `tests/test_task_create.py`, first extend the two test helpers so they carry `title`:

Update `_stub_enrichment` to accept and set a title:

```python
def _stub_enrichment(monkeypatch, key_points=None, links=None, due=None, title=None):
    summary_calls = []

    def fake_generate(event):
        summary_calls.append(event)
        return EmailSummary(key_points=key_points or [], relevant_links=links or [], title=title)

    monkeypatch.setattr(email_summary, "generate", fake_generate)
    deadline_calls = []

    def fake_deadline(event):
        deadline_calls.append(event)
        return due

    monkeypatch.setattr(deadline, "extract_deadline", fake_deadline)
    return summary_calls, deadline_calls
```

Update `_capture_create` to accept and record `title`:

```python
def _capture_create(monkeypatch, result="42"):
    created = {}

    def fake_create(event, *, tag_gids=None, due_date=None, html_notes="", title=None):
        created.update(tag_gids=tag_gids, due_date=due_date, html_notes=html_notes, title=title)
        if result is None:
            return None
        return CreatedTask(gid=result, permalink_url=f"https://a/{result}")

    monkeypatch.setattr(asana, "create_task", fake_create)
    return created
```

Then add two tests:

```python
def test_handle_passes_enriched_title(monkeypatch):
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch, title="Review Q3 board deck")
    created = _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)

    task_create.handle(make_email_event(importance="P1"))

    assert created["title"] == "[P1] Review Q3 board deck"


def test_handle_passes_none_title_when_unenriched(monkeypatch):
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch)  # title defaults to None
    created = _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)

    task_create.handle(make_email_event())

    assert created["title"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_task_create.py::test_handle_passes_enriched_title -v`
Expected: FAIL — `created["title"]` is `None` (handler doesn't build/pass a title yet).

- [ ] **Step 3: Build and pass the title in the handler**

In `handlers/task_create.py`, after the `html_notes` is built and before the `asana.create_task(...)` call (currently lines 33-41), compute the title and add it to the call:

```python
    html_notes = task_content.render_html_notes(
        task_content.for_email(event, key_points, relevant_links)
    )
    title = f"[{event['importance']}] {summary.title}" if summary.title else None
    task = asana.create_task(
        event,
        tag_gids=tag_gids,
        due_date=due_date,
        html_notes=html_notes,
        title=title,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_task_create.py -q`
Expected: PASS (new tests plus existing handler tests, which now pass `title=None` through the updated `_capture_create`).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS — no regressions across the suite.

- [ ] **Step 6: Commit**

```bash
git add handlers/task_create.py tests/test_task_create.py
git commit -m "feat: wire enriched [PX] {verb} {object} title through task_create"
```

---

### Task 4: Document the title standard and reference it

**Files:**
- Modify: `CLAUDE.md` (add a "Task title standard" note)
- Modify: `.claude/skills/tasks-architecture/SKILL.md` (reference the standard)
- Modify: `.claude/skills/verifying-pr-locally/SKILL.md` (what a correct title looks like in E2E output)

(Note: `~/.claude/skills/editing-tasks/SKILL.md` is a user-global skill outside this repo. Update it separately if desired — it is not part of this repo's commit.)

- [ ] **Step 1: Add the standard to `CLAUDE.md`**

Insert a new section immediately after the "## Task policy" section:

```markdown
## Task title standard

Pipeline task titles are `[PX] {verb} {object}` — a context-driven action, not
the raw subject. The `{verb} {object}` is generated by the `email_summary`
Haiku call (`services/email_summary.py`); the `[PX]` prefix is added in
`handlers/task_create.py`. Falls back to `[PX] {subject}` if enrichment yields
no title. Canonical spec:
`docs/superpowers/specs/2026-07-20-task-title-enrichment-design.md`.
```

- [ ] **Step 2: Reference the standard in `tasks-architecture`**

Read `.claude/skills/tasks-architecture/SKILL.md`, find where task creation / shaping is described, and add one sentence:

```markdown
Task titles follow the standard `[PX] {verb} {object}` (verb-first action,
generated in `services/email_summary.py`; see CLAUDE.md "Task title standard").
```

- [ ] **Step 3: Reference the standard in `verifying-pr-locally`**

Read `.claude/skills/verifying-pr-locally/SKILL.md`, find where the E2E create output is described, and note the expected title shape:

```markdown
A created task's title should read as an action, e.g. `[P1] Review Q3 board
deck` (the `[PX] {verb} {object}` standard), not a raw email subject.
```

- [ ] **Step 4: Verify the references resolve**

Run: `grep -rl "Task title standard\|{verb} {object}" CLAUDE.md .claude/skills/`
Expected: lists `CLAUDE.md`, `tasks-architecture/SKILL.md`, `verifying-pr-locally/SKILL.md`.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .claude/skills/tasks-architecture/SKILL.md .claude/skills/verifying-pr-locally/SKILL.md
git commit -m "docs: document the [PX] {verb} {object} task title standard"
```

---

## Verification (before PR)

- [ ] Full unit suite green: `.venv/bin/pytest tests/ -q`
- [ ] Static CI + runtime E2E via the `verifying-pr-locally` skill — a **real** Asana create, confirming the enriched title lands as `[PX] {verb} {object}` (unit tests mock Asana, so only a real create proves the `name` field). Optionally run `.venv/bin/python scripts/test-task-create.py`.
- [ ] Open the PR with the `/pr-open` skill.
```

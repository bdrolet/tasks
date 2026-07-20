# Task Content Standard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every Asana task a consistent, scannable shape — structured attributes in native slots (title/section/tags/due), freeform substance in a fixed, code-rendered description template — across both the automated email→task pipeline and the manual tasks-api.

**Architecture:** One pure content model (`TaskContent` + `Source`) and one deterministic HTML renderer (`render_html_notes`) shared by both creation paths. The automated pipeline builds a `TaskContent` from the email event + enrichment via `task_content.for_email(...)`; the manual API builds one from structured request fields. `clients/asana.py` stops shaping content (layer fix) and just POSTs a rendered `html_notes` string.

**Tech Stack:** Python 3.13, FastAPI (tasks-api), pytest, httpx (Asana client). No new dependencies.

## Global Constraints

- Free Asana tier — **no custom fields**. Structured attributes use native slots only: title prefix (priority), section (category), tags (topic), `due_on`/`due_at`, assignee.
- Rendering is **deterministic code, never an LLM** — identical `TaskContent` must produce byte-identical HTML.
- `clients/` is I/O only; content shaping lives in `services/`. `services/` files take no direct HTTP. `models/` imports from no other layer.
- Asana `html_notes` requires a `<body>` root element.
- Run tests with `.venv/bin/pytest tests/ -q` (or a single test with `-v`).
- Commit messages end with the repo's trailer lines (Co-Authored-By + Claude-Session), matching recent commits. Work stays on the `task-content-standard` branch (already checked out) — never commit to `main`.

---

### Task 1: Content model — `models/task_content.py`

**Files:**
- Create: `models/task_content.py`
- Test: `tests/test_task_content.py`

**Interfaces:**
- Consumes: nothing (pure types).
- Produces:
  - `TaskContent(context: str | None = None, key_points: list[str] = [], links: list[tuple[str, str]] = [], action_items: list[tuple[str, str]] = [], source: "Source | None" = None)` — `links` are `(url, label)`, `action_items` are `(label, url)`.
  - `Source(origin: str, rows: list[tuple[str, str]] = [], links: list[tuple[str, str]] = [], note: str | None = None)` — `rows` are `(label, value)`, `links` are `(url, label)`.

- [ ] **Step 1: Write the failing test**

Add to a new file `tests/test_task_content.py`:

```python
from models.task_content import Source, TaskContent


def test_taskcontent_defaults_are_empty():
    c = TaskContent()
    assert c.context is None
    assert c.key_points == []
    assert c.links == []
    assert c.action_items == []
    assert c.source is None


def test_source_defaults():
    s = Source(origin="Email")
    assert s.origin == "Email"
    assert s.rows == []
    assert s.links == []
    assert s.note is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.task_content'`

- [ ] **Step 3: Write minimal implementation**

Create `models/task_content.py`:

```python
"""Pure content types for a task's description. No imports from other layers.

TaskContent is the shared shape rendered into Asana html_notes by
services/task_content.py. Structured attributes (priority, category, topic,
due date) are NOT here — they live in native Asana slots, not the description.
JSON has no tuples, so link/row pairs are (a, b) tuples built by callers.
"""

from dataclasses import dataclass, field


@dataclass
class Source:
    """The provenance footer: where the task came from."""

    origin: str  # e.g. "Email", "Created manually"
    rows: list[tuple[str, str]] = field(default_factory=list)  # (label, value): From, Received
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, label): Open in Outlook
    note: str | None = None  # freeform provenance note (automated: AI reasoning)


@dataclass
class TaskContent:
    context: str | None = None  # optional lead prose ("what / why")
    key_points: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, label)
    action_items: list[tuple[str, str]] = field(default_factory=list)  # (label, url)
    source: Source | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add models/task_content.py tests/test_task_content.py
git commit -m "$(cat <<'EOF'
tasks: add TaskContent + Source content model

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015ibLc8nRE5qFwFV3xpzcFM
EOF
)"
```

---

### Task 2: HTML renderer — `services/task_content.py::render_html_notes`

**Files:**
- Create: `services/task_content.py`
- Test: `tests/test_task_content.py` (append)

**Interfaces:**
- Consumes: `TaskContent`, `Source` from Task 1.
- Produces: `render_html_notes(content: TaskContent) -> str` — returns a `<body>…</body>` HTML string. Section order: context → key_points → links → action_items → source footer. Non-source sections omitted when empty; source footer always present (defaults to `Source: Created manually` when `content.source is None`). Escapes `&`, `<`, `>` in all text.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_content.py`:

```python
from services.task_content import render_html_notes


def test_render_empty_content_has_manual_source_footer():
    html = render_html_notes(TaskContent())
    assert html.startswith("<body>") and html.endswith("</body>")
    assert "<strong>Source:</strong> Created manually" in html
    # no substance sections when everything is empty
    assert "Key points" not in html
    assert "Links:" not in html
    assert "Actions" not in html


def test_render_sections_in_order_and_escaped():
    html = render_html_notes(
        TaskContent(
            context="why <this> & that",
            key_points=["point <one>"],
            links=[("https://x", "Doc & more")],
            action_items=[("Confirm review", "https://hook/label?x=1&y=2")],
            source=Source(
                origin="Email",
                rows=[("From", "Alice <a@x>")],
                links=[("https://outlook/1", "Open in Outlook")],
                note="Needs review",
            ),
        )
    )
    # escaping
    assert "why &lt;this&gt; &amp; that" in html
    assert "<li>point &lt;one&gt;</li>" in html
    assert '<a href="https://x">Doc &amp; more</a>' in html
    # ordering: context before key points before links before actions before source
    assert (
        html.index("why &lt;this")
        < html.index("point &lt;one")
        < html.index("Doc &amp; more")
        < html.index("Confirm review")
        < html.index("<strong>Source:</strong>")
    )
    assert "<strong>Source:</strong> Email" in html
    assert "<li><strong>From:</strong> Alice &lt;a@x&gt;</li>" in html
    assert '<a href="https://outlook/1">Open in Outlook</a>' in html
    assert "Needs review" in html


def test_render_is_deterministic():
    c = TaskContent(key_points=["a", "b"], source=Source(origin="Email"))
    assert render_html_notes(c) == render_html_notes(c)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: FAIL — `ImportError: cannot import name 'render_html_notes'`

- [ ] **Step 3: Write minimal implementation**

Create `services/task_content.py`:

```python
"""Build and render a task's description content.

render_html_notes turns a TaskContent into the Asana html_notes string —
deterministic code, never an LLM (a scannable standard needs identical output
for identical input). for_email (Task 3) builds the email-derived TaskContent.
"""

from models.task_content import Source, TaskContent


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_source(source: Source | None) -> str:
    if source is None:
        return "<strong>Source:</strong> Created manually"
    parts = [f"<strong>Source:</strong> {_esc(source.origin)}"]
    if source.rows:
        parts.append(
            "<ul>"
            + "".join(
                f"<li><strong>{_esc(label)}:</strong> {_esc(value)}</li>"
                for label, value in source.rows
            )
            + "</ul>"
        )
    if source.links:
        parts.append(
            "<ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>' for url, label in source.links
            )
            + "</ul>"
        )
    if source.note:
        parts.append(f"<p>{_esc(source.note)}</p>")
    return "".join(parts)


def render_html_notes(content: TaskContent) -> str:
    parts: list[str] = []
    if content.context:
        parts.append(f"<p>{_esc(content.context)}</p>")
    if content.key_points:
        parts.append(
            "<strong>Key points:</strong><ul>"
            + "".join(f"<li>{_esc(p)}</li>" for p in content.key_points)
            + "</ul>"
        )
    if content.links:
        parts.append(
            "<strong>Links:</strong><ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>' for url, label in content.links
            )
            + "</ul>"
        )
    if content.action_items:
        parts.append(
            "<strong>Actions</strong><ul>"
            + "".join(
                f'<li><a href="{_esc(url)}">{_esc(label)}</a></li>'
                for label, url in content.action_items
            )
            + "</ul>"
        )
    parts.append(_render_source(content.source))
    return "<body>" + "".join(parts) + "</body>"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add services/task_content.py tests/test_task_content.py
git commit -m "$(cat <<'EOF'
tasks: add deterministic html_notes renderer

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015ibLc8nRE5qFwFV3xpzcFM
EOF
)"
```

---

### Task 3: Email content builder — `services/task_content.py::for_email`

**Files:**
- Modify: `services/task_content.py` (add `for_email` + `_action_url`)
- Test: `tests/test_task_content.py` (append)

**Interfaces:**
- Consumes: `EmailClassifiedEvent` (from `models.events`), the merged `key_points: list[str]` and `relevant_links: list[list[str]]` the handler computes.
- Produces: `for_email(event, key_points, relevant_links) -> TaskContent`. Builds:
  - `key_points` from the arg (or empty), and `context` = a ≤500-char body preview only when `key_points` is empty.
  - `links` = `relevant_links` as `(url, label)` tuples.
  - `action_items` = category-dependent confirm/alt/reference/ignore label links (+ draft-reply link when `event["draft_link"]` is set).
  - `source` = `Source(origin="Email", rows=[("From", …), ("Received", …)], links=[(web_link, "Open in Outlook")] if web_link, note=event["reasoning"])`.

Reads env `WEBHOOK_URL` and `WEBHOOK_LABEL_TOKEN` for the action-link URLs (moved verbatim from the old `clients/asana.py::_html_notes`). Builds raw strings — the renderer does all escaping.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_task_content.py`:

```python
from services.task_content import for_email
from tests.test_events import make_email_event


def _action_labels(content):
    return [label for label, _ in content.action_items]


def test_for_email_review_action_items_and_source(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    monkeypatch.setenv("WEBHOOK_LABEL_TOKEN", "tok")
    content = for_email(
        make_email_event(category="review"),
        key_points=["Point one"],
        relevant_links=[["https://x", "Doc"]],
    )
    assert content.key_points == ["Point one"]
    assert content.context is None  # key points present → no preview
    assert content.links == [("https://x", "Doc")]
    assert _action_labels(content) == [
        "Confirmed review",
        "Respond instead",
        "Reference",
        "Ignore",
    ]
    # action URLs carry the message id, chosen label, source, and token
    confirm_url = content.action_items[0][1]
    assert "id=msg-123" in confirm_url and "label=review" in confirm_url
    assert "source=human_confirmation" in confirm_url and "token=tok" in confirm_url
    assert content.source.origin == "Email"
    assert content.source.rows == [
        ("From", "Alice (alice@example.com)"),
        ("Received", "2026-07-15T12:00:00Z"),
    ]
    assert content.source.links == [("https://outlook.example/msg-123", "Open in Outlook")]
    assert content.source.note == "Needs review"


def test_for_email_respond_swaps_confirm_and_alt(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    content = for_email(make_email_event(category="respond"), key_points=["p"], relevant_links=[])
    assert _action_labels(content)[:2] == ["Confirmed respond", "Review instead"]


def test_for_email_preview_fallback_without_key_points(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    content = for_email(make_email_event(body="y" * 900), key_points=[], relevant_links=[])
    assert content.key_points == []
    assert content.context is not None
    assert content.context.startswith("y" * 500)
    assert content.context.endswith("...")


def test_for_email_includes_draft_reply_action(monkeypatch):
    monkeypatch.setenv("WEBHOOK_URL", "https://hook")
    content = for_email(
        make_email_event(draft_link="https://outlook/draft"),
        key_points=["p"],
        relevant_links=[],
    )
    assert ("Open draft reply in Outlook", "https://outlook/draft") in content.action_items
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: FAIL — `ImportError: cannot import name 'for_email'`

- [ ] **Step 3: Write minimal implementation**

Add to the top of `services/task_content.py` (imports) and below `render_html_notes`:

```python
import os
import urllib.parse

from models.events import EmailClassifiedEvent
```

```python
def _action_url(message_id: str, label: str, source: str) -> str:
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    label_token = os.environ.get("WEBHOOK_LABEL_TOKEN", "")
    params = f"id={message_id}&label={label}&source={source}"
    if label_token:
        params += f"&token={urllib.parse.quote(label_token, safe='')}"
    return f"{webhook_url}/label?{params}"


def for_email(
    event: EmailClassifiedEvent,
    key_points: list[str],
    relevant_links: list[list[str]],
) -> TaskContent:
    """Build the email-derived TaskContent (substance + provenance footer)."""
    message_id = event["message_id"]

    context = None
    if not key_points:
        body = event["body"] or ""
        context = body[:500] + ("..." if len(body) > 500 else "")

    if event["category"] == "respond":
        confirm_label, confirm_text = "respond", "Confirmed respond"
        alt_label, alt_text = "review", "Review instead"
    else:
        confirm_label, confirm_text = "review", "Confirmed review"
        alt_label, alt_text = "respond", "Respond instead"

    action_items: list[tuple[str, str]] = [
        (confirm_text, _action_url(message_id, confirm_label, "human_confirmation")),
        (alt_text, _action_url(message_id, alt_label, "human_correction")),
        ("Reference", _action_url(message_id, "reference", "human_correction")),
        ("Ignore", _action_url(message_id, "ignore", "human_correction")),
    ]
    draft_link = event.get("draft_link")
    if draft_link:
        action_items.append(("Open draft reply in Outlook", draft_link))

    rows = [
        ("From", f"{event['sender_display']} ({event['sender']})"),
        ("Received", event["received_at"]),
    ]
    links = [(event["web_link"], "Open in Outlook")] if event.get("web_link") else []
    source = Source(origin="Email", rows=rows, links=links, note=event["reasoning"])

    return TaskContent(
        context=context,
        key_points=list(key_points),
        links=[(url, label) for url, label in relevant_links],
        action_items=action_items,
        source=source,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_task_content.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: Commit**

```bash
git add services/task_content.py tests/test_task_content.py
git commit -m "$(cat <<'EOF'
tasks: build email-derived TaskContent (for_email)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015ibLc8nRE5qFwFV3xpzcFM
EOF
)"
```

---

### Task 4: Wire the automated pipeline — `clients/asana.py` + `handlers/task_create.py`

**Files:**
- Modify: `clients/asana.py` (remove `_html_notes`, `_esc`; change `create_task` signature; imports)
- Modify: `handlers/task_create.py` (build + render content; new `create_task` call)
- Modify: `tests/test_asana_client.py` (create_task tests)
- Modify: `tests/test_task_create.py` (handler test stub + assertions)

**Interfaces:**
- Consumes: `task_content.for_email`, `task_content.render_html_notes` (Tasks 2–3).
- Produces: `asana.create_task(event, *, tag_gids: list[str] | None = None, due_date: str | None = None, html_notes: str = "") -> CreatedTask | None` — no longer builds notes; POSTs the passed `html_notes`. Title, `external` dedup, projects, `due_on`, `tags` behavior unchanged.

- [ ] **Step 1: Update the client tests to the new signature (failing)**

In `tests/test_asana_client.py`, replace `test_create_task_builds_payload`, `test_create_task_key_points_render`, and `test_create_task_preview_fallback_without_key_points` with:

```python
def test_create_task_builds_payload(monkeypatch):
    calls = _capture(
        monkeypatch, _resp(201, {"data": {"gid": "42", "permalink_url": "https://a/42"}})
    )
    task = asana.create_task(
        make_email_event(), tag_gids=["tg1"], due_date="2026-07-20", html_notes="<body>hi</body>"
    )
    assert task is not None and task.gid == "42"
    payload = calls[0]["json"]["data"]
    assert payload["name"] == "[P1] Quarterly report"
    assert payload["external"] == {"gid": "msg-123", "data": "inbox"}
    assert payload["due_on"] == "2026-07-20"
    assert payload["tags"] == ["tg1"]
    assert payload["projects"] == ["proj-1"]
    assert payload["html_notes"] == "<body>hi</body>"  # passed through, not built here
```

(Leave `test_create_task_duplicate_returns_none` and `test_create_task_unconfigured_returns_none` as-is — they call `create_task(make_email_event())` with the new `html_notes=""` default.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_asana_client.py -q`
Expected: FAIL — `create_task()` got unexpected/old keyword args or `_html_notes`-built notes mismatch.

- [ ] **Step 3: Change `clients/asana.py`**

Delete the `_esc` function (lines ~93–94) and the entire `_html_notes` function (lines ~97–183) — both are used only inside `_html_notes`. Remove the now-unused `import urllib.parse` (its only use was inside `_html_notes`). **Keep `from datetime import date`** — it is still used elsewhere in the file (`date.today()` at ~line 318). Then change `create_task`:

```python
def create_task(
    event: EmailClassifiedEvent,
    *,
    tag_gids: list[str] | None = None,
    due_date: str | None = None,
    html_notes: str = "",
) -> CreatedTask | None:
    """Create an Asana task from an email_classified event. The description is
    pre-rendered by the caller (handlers/task_create.py via services/task_content).
    Returns None if Asana is not configured or a task for this message_id already
    exists."""
    if not ASANA_API_KEY or not ASANA_PROJECT_ID:
        return None

    payload: dict = {
        "name": f"[{event['importance']}] {event['subject'] or '(no subject)'}",
        "html_notes": html_notes,
        "projects": [ASANA_PROJECT_ID],
        "external": {"gid": event["message_id"], "data": "inbox"},
    }
    if due_date:
        payload["due_on"] = due_date
    if tag_gids:
        payload["tags"] = tag_gids

    resp = _request(
        "POST",
        "/tasks",
        operation="create_task",
        params={"opt_fields": "gid,permalink_url"},
        json={"data": payload},
    )
    if resp.status_code == 400:
        errs = resp.json().get("errors", [])
        if any("already assigned" in e.get("message", "").lower() for e in errs):
            logger.warning(
                "Asana task for message_id=%s already exists (duplicate external.gid) — skipping",
                event["message_id"],
            )
            return None
    resp.raise_for_status()
    data = resp.json()["data"]
    return CreatedTask(gid=data["gid"], permalink_url=data["permalink_url"])
```

- [ ] **Step 4: Run client tests**

Run: `.venv/bin/pytest tests/test_asana_client.py -q`
Expected: PASS

- [ ] **Step 5: Update the handler**

In `handlers/task_create.py`, change the import line `from services import deadline, email_summary, policy, sections, tags` to add `task_content`, and rewrite the enrichment/create block:

```python
    # Enrichment: generated summary first, invite seeds from inbox appended.
    summary = email_summary.generate(event)
    key_points = summary.key_points + (event.get("seed_key_points") or [])
    relevant_links = summary.relevant_links + (event.get("seed_links") or [])

    due_date = None
    if event["importance"] in ("P0", "P1"):
        try:
            due_date = deadline.extract_deadline(event)
        except Exception:
            logger.exception("Deadline extraction failed for message_id=%s", event["message_id"])

    tag_gids = tags.resolve_gids(event.get("tags") or [])
    html_notes = task_content.render_html_notes(
        task_content.for_email(event, key_points, relevant_links)
    )
    task = asana.create_task(
        event,
        tag_gids=tag_gids,
        due_date=due_date,
        html_notes=html_notes,
    )
```

- [ ] **Step 6: Update the handler test**

In `tests/test_task_create.py`, replace `_capture_create` and the assertions in `test_handle_enriches_creates_places_and_stores`:

```python
def _capture_create(monkeypatch, result="42"):
    created = {}

    def fake_create(event, *, tag_gids=None, due_date=None, html_notes=""):
        created.update(tag_gids=tag_gids, due_date=due_date, html_notes=html_notes)
        if result is None:
            return None
        return CreatedTask(gid=result, permalink_url=f"https://a/{result}")

    monkeypatch.setattr(asana, "create_task", fake_create)
    return created
```

In `test_handle_enriches_creates_places_and_stores`, replace the `created["key_points"]` / `created["relevant_links"]` assertions (lines ~71–72) with assertions on the rendered notes:

```python
    assert created["tag_gids"] == ["tg1"]
    # merged summary + seed key points render into the description
    assert "<li>Summarized point</li>" in created["html_notes"]
    assert "<li>Calendar invite: Standup</li>" in created["html_notes"]
    assert '<a href="https://x">Doc</a>' in created["html_notes"]
    assert '<a href="https://z">RSVP: Accept</a>' in created["html_notes"]
    assert created["due_date"] == "2026-07-31"  # P1 → deadline extraction ran
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS (all tests, including the new content tests)

- [ ] **Step 8: Commit**

```bash
git add clients/asana.py handlers/task_create.py tests/test_asana_client.py tests/test_task_create.py
git commit -m "$(cat <<'EOF'
tasks: render pipeline task notes via TaskContent; asana client is I/O only

create_task now takes a pre-rendered html_notes string; _html_notes and the
action-link construction move to services/task_content.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015ibLc8nRE5qFwFV3xpzcFM
EOF
)"
```

---

### Task 5: Restructure the manual API — `api/routers/tasks.py`

**Files:**
- Modify: `api/routers/tasks.py` (request models, `create_task`, `patch_task`)
- Modify: `tests/test_api_tasks.py` (create/patch tests)

**Interfaces:**
- Consumes: `services.task_content.render_html_notes`, `models.task_content.TaskContent`.
- Produces: `CreateTaskRequest` / `UpdateTaskRequest` with structured content fields (`context`, `key_points`, `links`, `action_items`) and optional `priority` (`P0`–`P3`) instead of `description` / `html_description`. Title = `[{priority}] {name}` when `priority` set, else `name`. **`priority` requires `name` in the same request** (400 otherwise). PATCH: any content field present → description is rewritten (replace, not merge) from the content fields; absent → untouched.

Note: `wrap_html_body` **stays** — `api/routers/comments.py` still uses it for comment bodies. Only the description code stops calling it (the renderer wraps `<body>` itself).

- [ ] **Step 1: Write the failing tests**

In `tests/test_api_tasks.py`, replace the create/patch-description tests. Remove `test_create_task_html_description_wrapped` and `test_create_task_rejects_both_descriptions`; replace `test_create_task_defaults_to_email_project` and `test_patch_omitted_fields_untouched`; and add new cases:

```python
def test_create_task_renders_structured_content(monkeypatch):
    captured = {}

    def fake_create(fields):
        captured.update(fields)
        return CreatedTask(gid="t9", permalink_url="https://a/t9")

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)

    resp = client.post(
        "/tasks",
        json={
            "name": "Renew passport",
            "context": "expires in October",
            "key_points": ["book appointment"],
            "due_on": "2026-08-01",
        },
        headers=AUTH,
    )
    assert resp.status_code == 201
    assert captured["projects"] == ["p-email"]
    assert captured["due_on"] == "2026-08-01"
    assert captured["name"] == "Renew passport"  # no priority → plain title
    assert captured["html_notes"].startswith("<body>")
    assert "expires in October" in captured["html_notes"]
    assert "<li>book appointment</li>" in captured["html_notes"]
    assert "Created manually" in captured["html_notes"]  # mandatory source footer


def test_create_task_empty_content_gets_source_footer(monkeypatch):
    captured = {}
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: captured.update(fields) or CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    client.post("/tasks", json={"name": "Bare task"}, headers=AUTH)
    assert captured["html_notes"] == "<body><strong>Source:</strong> Created manually</body>"


def test_create_task_priority_prefixes_title(monkeypatch):
    captured = {}
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: captured.update(fields) or CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    client.post("/tasks", json={"name": "Ship it", "priority": "P1"}, headers=AUTH)
    assert captured["name"] == "[P1] Ship it"


def test_patch_rewrites_description_from_content(monkeypatch):
    captured = _patch_env(monkeypatch)
    client.patch("/tasks/t1", json={"key_points": ["new point"]}, headers=AUTH)
    assert "<li>new point</li>" in captured["update"]["html_notes"]


def test_patch_without_content_leaves_description(monkeypatch):
    captured = _patch_env(monkeypatch)
    client.patch("/tasks/t1", json={"name": "New name"}, headers=AUTH)
    assert captured["update"] == {"name": "New name"}  # no html_notes touched
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/pytest tests/test_api_tasks.py -q`
Expected: FAIL — requests send unknown fields / `description` handling gone.

- [ ] **Step 3: Rewrite the request models and endpoints**

In `api/routers/tasks.py`, add imports near the top:

```python
from models.task_content import TaskContent
from services.task_content import render_html_notes
```

Replace `CreateTaskRequest` and `UpdateTaskRequest`:

```python
class CreateTaskRequest(BaseModel):
    name: str = Field(min_length=1)
    priority: str | None = None  # "P0".."P3" — prefixes the title
    context: str | None = None
    key_points: list[str] = []
    links: list[tuple[str, str]] = []  # (url, label)
    action_items: list[tuple[str, str]] = []  # (label, url)
    project: str | None = None  # name or GID; default = configured tasks project
    section: str | None = None  # name or GID within the project
    due_on: str | None = None
    due_at: str | None = None
    tags: list[str] = []
    assignee: str | None = None  # "me", email, or GID — passed through to Asana


class UpdateTaskRequest(BaseModel):
    name: str | None = None
    priority: str | None = None  # requires name in the same request
    context: str | None = None
    key_points: list[str] = []
    links: list[tuple[str, str]] = []
    action_items: list[tuple[str, str]] = []
    completed: bool | None = None
    due_on: str | None = None  # explicit null clears
    due_at: str | None = None  # explicit null clears
    section: str | None = None
    add_tags: list[str] = []
    remove_tags: list[str] = []
    assignee: str | None = None  # explicit null unassigns
```

Add a helper near `wrap_html_body`:

```python
_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}


def _title(name: str, priority: str | None) -> str:
    if priority is None:
        return name
    if priority not in _VALID_PRIORITIES:
        raise HTTPException(status_code=400, detail=f"invalid priority: {priority}")
    return f"[{priority}] {name}"


def _content_fields_set(body) -> bool:
    """True if any description-content field was provided in the request."""
    return bool(body.model_fields_set & {"context", "key_points", "links", "action_items"})


def _render_content(body) -> str:
    return render_html_notes(
        TaskContent(
            context=body.context,
            key_points=body.key_points,
            links=[tuple(pair) for pair in body.links],
            action_items=[tuple(pair) for pair in body.action_items],
        )
    )
```

Replace the body of `create_task` (drop the description/html_description branching):

```python
@router.post("/tasks", response_model=CreatedTaskResponse, status_code=201)
def create_task(body: CreateTaskRequest, _: None = Depends(verify_token)) -> CreatedTaskResponse:
    with translate_asana_errors():
        project_gid = _resolve_project_gid(body.project)

        fields: dict = {
            "name": _title(body.name, body.priority),
            "projects": [project_gid],
            "html_notes": _render_content(body),
        }
        if body.due_on is not None:
            fields["due_on"] = body.due_on
        if body.due_at is not None:
            fields["due_at"] = body.due_at
        if body.assignee is not None:
            fields["assignee"] = body.assignee
        if body.tags:
            gids = tags_service.resolve_gids(body.tags)
            if gids:
                fields["tags"] = gids

        section_gid = resolve_section(project_gid, body.section) if body.section else None
        created = asana.create_task_from_fields(fields)
        if section_gid:
            asana.add_task_to_section(created.gid, section_gid)

    return CreatedTaskResponse(task_gid=created.gid, permalink_url=created.permalink_url)
```

Replace the field-building part of `patch_task` (keep the section/tags blocks below it unchanged):

```python
@router.patch("/tasks/{gid}")
def patch_task(gid: str, body: UpdateTaskRequest, _: None = Depends(verify_token)) -> dict:
    if body.priority is not None and body.name is None:
        raise HTTPException(status_code=400, detail="priority requires name in the same request")

    with translate_asana_errors():
        task = asana.get_task_detail(gid)
        if task is None:
            raise HTTPException(status_code=404, detail=f"unknown task: {gid}")

        fields: dict = {}
        if body.name is not None:
            fields["name"] = _title(body.name, body.priority)
        if _content_fields_set(body):
            fields["html_notes"] = _render_content(body)
        if body.completed is not None:
            fields["completed"] = body.completed
        # Nullable fields: explicit null in the request body clears the value.
        for field in ("due_on", "due_at", "assignee"):
            if field in body.model_fields_set:
                fields[field] = getattr(body, field)
        if fields:
            asana.update_task(gid, fields)
        # ... (section move + add_tags/remove_tags blocks stay exactly as they are)
```

Leave the section-move and tag blocks in `patch_task` unchanged. Delete `wrap_html_body`'s use inside this router's create/patch (already removed above); **keep the `wrap_html_body` function itself** — `api/routers/comments.py` imports it.

- [ ] **Step 4: Run the API tests**

Run: `.venv/bin/pytest tests/test_api_tasks.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add api/routers/tasks.py tests/test_api_tasks.py
git commit -m "$(cat <<'EOF'
tasks-api: structured task content + optional priority prefix

CreateTaskRequest/UpdateTaskRequest take context/key_points/links/action_items
(rendered via services/task_content) instead of a free-form description blob;
optional priority prefixes the title.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015ibLc8nRE5qFwFV3xpzcFM
EOF
)"
```

---

### Task 6: Update the standard doc and the editing-tasks skill

**Files:**
- Create: `docs/task-content-standard.md`
- Modify: `~/.claude/skills/editing-tasks/SKILL.md`

**Interfaces:**
- Consumes: the conventions from the spec (§6) and the new request shape from Task 5.
- Produces: reference documentation. No code.

- [ ] **Step 1: Write the standard doc**

Create `docs/task-content-standard.md`:

```markdown
# Task content standard

Every task in the configured Asana project follows this shape so a reader
always knows where to look. Structured attributes live in native Asana slots;
the description holds prose in a fixed order.

## Where each thing goes

| Information | Slot |
|---|---|
| Priority (P0–P3) | Title prefix `[P1] …` (automatic on email tasks; optional on manual) |
| Category (urgent/respond/review) | Section |
| Topic | Tags (kebab-case: `finance`, `passport-renewal`) |
| Due date | `due_on` (or `due_at` only when time-of-day matters) |
| Assignee | Native assignee |
| Origin / provenance | Description **footer** (`Source: …`) |
| Substance (summary, links, next action) | Description template |

## Description template (fixed order)

`context → key points → links → actions → source footer`. Sections omit when
empty; the source footer is always present (`Created manually` for manual tasks).

## Conventions

- **Priority** — the `[P0–P3]` title prefix, nothing else. Don't put priority
  in a tag or a description line.
- **Tags** — kebab-case topic words only. Not status, not priority (those have
  their own slots).
- **Due dates** — set only when a real date is known; never fabricate one.
- **Comments** — terse, single-purpose status updates ("waiting on Alice"), not
  a restatement of the description.
```

- [ ] **Step 2: Update the editing-tasks skill**

In `~/.claude/skills/editing-tasks/SKILL.md`, replace the "Create a task" example body and the "Optional fields" paragraph (lines ~24–33) with the structured shape:

```bash
curl -s -XPOST "$BASE/tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Renew passport","context":"expires in Oct","key_points":["book appointment"],"due_on":"2026-08-01"}'
# -> {"task_gid":"...","permalink_url":"..."}
```

Optional fields: `priority` (`P0`–`P3` — prefixes the title), `context`
(lead prose), `key_points` (list), `links` (list of `[url, label]`),
`action_items` (list of `[label, url]`), `project` (name or GID — defaults to
the configured tasks project), `section` (name or GID), `due_at` (ISO datetime,
instead of `due_on`), `tags` (kebab-case topic names — created if missing),
`assignee` (`"me"`, an email, or a GID). There is no free-form `description` —
the API renders the description from these fields so every task looks the same.

Update the PATCH section's `{"name": "New title", "description": "new body"}`
example to `{"key_points": ["new point"]}` (any content field rewrites the
description), and note: **to re-prioritize, send `name` and `priority` together**
(priority alone is rejected).

- [ ] **Step 3: Commit the repo doc**

```bash
git add docs/task-content-standard.md
git commit -m "$(cat <<'EOF'
docs: add task content standard reference

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015ibLc8nRE5qFwFV3xpzcFM
EOF
)"
```

(The `editing-tasks` skill lives outside the repo — it is a standalone
filesystem edit, not part of this commit.)

- [ ] **Step 4: Final full-suite run**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS (all)

---

## Notes / decisions for the reviewer

- **`priority` on PATCH requires `name`.** The spec put optional priority on both
  request bodies but left update-semantics unspecified. Rebuilding a title prefix
  on a bare `priority` patch would mean reading the current name and stripping any
  existing prefix — fiddly and error-prone. Instead, priority always composes with
  a provided `name` (both paths), and a `priority`-only PATCH is a 400. Flag if you
  want prefix-stripping instead.
- **`wrap_html_body` is retained**, not deleted — `api/routers/comments.py` still
  uses it. Only the description path stopped calling it (the renderer wraps
  `<body>` itself). The spec's "drop if unused" resolved to "still used → keep."
- **To/Cc dropped** from the automated task description (per spec §4). If you want
  them, add them as `Source.rows` in `for_email` (Task 3).
- **Preview fallback** (email with no extracted key points) is preserved as the
  `context` paragraph rather than the old labeled "Preview:" block.
- **Structured-outputs enrichment** (spec's optional follow-up) is intentionally
  **not** in this plan — it's a separable reliability change to `email_summary` and
  doesn't affect the standard. Ship it as its own plan if wanted.
```

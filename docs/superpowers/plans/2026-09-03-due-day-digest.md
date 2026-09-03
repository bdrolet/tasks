# Due-Day Digest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One all-day Google Calendar event per day that has open Asana tasks due, on the right calendar (Family Board → Family; `cheryl` tag → "Ben | Cheryl"; else primary), listing each task as a link plus 2–3 Claude-condensed bullets and its doc links.

**Architecture:** The tasks repo owns the digest: a `POST /digest` route on the webhook CF, ticked every 10 minutes by Cloud Scheduler, rebuilds a rolling 30-day window when the Asana webhook has set a dirty flag or the last rebuild is over an hour old. It lists Asana live, routes and orders tasks, condenses bullets through Haiku with a per-task cache, diffs against a `due_day_events` table, and applies creates/patches/deletes through `schedule-api`. `schedule-api` (separate repo, `~/src/schedule`) gains a structured `sections` description field rendered as HTML so titles are real links.

**Tech Stack:** Python 3.13, `httpx`, `psycopg`/pg8000 via `clients/db.py`, Anthropic SDK via `clients/claude.py` (Haiku), FastAPI + pydantic (schedule-api), Terraform (Cloud Scheduler, Secret Manager data source), pytest.

**Spec:** `docs/superpowers/specs/2026-09-03-due-day-digest-design.md`

## Global Constraints

- Layer rules (CLAUDE.md): `clients/` I/O only; `repo/` takes an open connection; `services/` no direct HTTP; `handlers/` called only from `main.py`; `models/` import nothing from other layers.
- Every Asana call goes through `clients/asana.py::_request`.
- Metrics are prefixed `asana.` in OTel (`asana_` in Grafana).
- Window: `[today, today + 30]` inclusive, *today* in `America/Los_Angeles`.
- Routing precedence: Family Board project → `cheryl` tag → primary. Env: `ASANA_PROJECT_FAMILY_GID`, `CALENDAR_FAMILY_ID`, `CALENDAR_SHARED_ID`.
- Event title: `1 task due` / `N tasks due`. Transparency `transparent`. All-day (`date`).
- Bullets: Haiku via `clients.claude.summarize`, cache in `task_bullets`, cap `DIGEST_BULLET_CALLS_MAX = 40` calls per rebuild, fallback never cached.
- Links: `<a>` tags inside the description's `Links:` block only, de-duplicated, cap 5.
- Rebuild decision: dirty (`dirty_at > last_rebuilt_at`), stale (`last_rebuilt_at` older than 60 min or NULL), or `{"force": true}`.
- DB failure at the decision or listing step skips the rebuild (never risk duplicate events).
- Asana webhook must return fast: the webhook only sets the dirty flag.
- Personal identifiers (calendar ids, project GIDs) live in gitignored `terraform.tfvars` and GitHub repo variables, never in committed files.
- Run tests with `.venv/bin/pytest tests/ -q` from each repo root. Commit after each task; never commit to `main` (tasks branch: `due-day-digest`; schedule branch: `event-sections`).

---

## File map

**schedule repo (`~/src/schedule`, branch `event-sections`)**

| File | Change |
|---|---|
| `services/event_content.py` | `render_description(..., sections=None)` HTML path; `build_body(..., sections=None)` |
| `api/routers/events.py` | `Section` model; `sections` on `CreateEventRequest`/`PatchEventRequest`; `CONTENT_FIELDS` gains `sections` |
| `docs/event-content-standard.md` | "Digest" paragraph |
| `tests/test_event_content.py`, `tests/test_api_events.py` | new tests |

**tasks repo (`~/src/tasks`, branch `due-day-digest`)**

| File | Responsibility |
|---|---|
| `models/digest.py` | `DigestTask`, `DigestEvent`, `Plan` dataclasses |
| `services/due_digest.py` | pure: `today_local`, `route`, `in_window`, `order`, `title_for`, `build_events`, `plan` |
| `services/task_bullets.py` | `parse_links`, `description_text`, `fallback_points`, `content_hash`, `Budget`, `points_for` (Haiku + cache protocol) |
| `repo/due_digest.py` | `due_day_events`, `task_bullets`, `digest_state` access |
| `repo/schema.sql` | three new tables |
| `clients/schedule_api.py` | HTTP to schedule-api |
| `clients/asana.py` | `DIGEST_OPT_FIELDS`; `opt_fields=` on the two list functions |
| `clients/otel.py` | four new counters |
| `handlers/due_digest.py` | `run(force)`: decision, listing, bullets, plan, execute, prune |
| `handlers/asana_webhook.py` | set the dirty flag |
| `main.py` | `POST /digest` |
| `terraform/*` | variables, secret data source + IAM, env, scheduler job, timeout |
| `.github/workflows/deploy.yml` | new `TF_VAR_*` lines |
| `scripts/fetch-env.sh`, `scripts/test-digest.py`, `terraform/terraform.tfvars.example` | local dev |
| `CLAUDE.md`, `docs/asana-webhook-setup.md` | docs |

---

### Task 1: schedule-api — HTML `sections` rendering in `event_content`

**Files:**
- Modify: `~/src/schedule/services/event_content.py`
- Test: `~/src/schedule/tests/test_event_content.py`

**Interfaces:**
- Produces: `render_description(context, key_points, links, sections=None) -> str | None` where `sections` is `list[dict]` with keys `title: str`, `url: str | None`, `points: list[str]`, `links: list[tuple[str, str]]` (url, label). Non-empty `sections` → HTML string; otherwise unchanged plain text. `build_body(..., sections=None)` passes it through.

- [ ] **Step 1: Create the branch**

```bash
cd ~/src/schedule && git checkout main && git pull -q && git checkout -b event-sections
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_event_content.py`:

```python
def test_render_description_sections_render_as_html_with_linked_titles():
    out = ec.render_description(
        None,
        [],
        [],
        sections=[
            {
                "title": "[P1] Renew passport",
                "url": "https://app.asana.com/0/1/2",
                "points": ["Appointment 2026-09-12", "Bring DS-82 & photos"],
                "links": [("https://drive.google.com/d/1", "DS-82 (filled)")],
            }
        ],
    )
    assert out == (
        '<b><a href="https://app.asana.com/0/1/2">[P1] Renew passport</a></b>'
        "<ul><li>Appointment 2026-09-12</li><li>Bring DS-82 &amp; photos</li>"
        '<li><a href="https://drive.google.com/d/1">DS-82 (filled)</a></li></ul>'
    )


def test_render_description_section_without_url_is_bold_only_and_escapes():
    out = ec.render_description(
        "Due <today>", [], [], sections=[{"title": "A & B", "url": None, "points": [], "links": []}]
    )
    assert out == "<p>Due &lt;today&gt;</p><b>A &amp; B</b>"


def test_render_description_sections_then_top_level_points_and_links():
    out = ec.render_description(
        None,
        ["top point"],
        [("https://x", "x")],
        sections=[{"title": "T", "url": None, "points": ["p"], "links": []}],
    )
    assert out == '<b>T</b><ul><li>p</li></ul><ul><li>top point</li></ul><p>Links:<br><a href="https://x">x</a></p>'


def test_render_description_empty_sections_keeps_plain_text():
    assert ec.render_description(None, ["only a point"], [], sections=[]) == "- only a point"
    assert ec.render_description(None, [], [], sections=None) is None


def test_build_body_passes_sections_into_description():
    body = ec.build_body(
        title="2 tasks due",
        date="2026-09-10",
        timezone="America/Los_Angeles",
        sections=[{"title": "T", "url": "https://a", "points": ["p"], "links": []}],
    )
    assert body["description"] == '<b><a href="https://a">T</a></b><ul><li>p</li></ul>'
    assert body["start"] == {"date": "2026-09-10"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd ~/src/schedule && .venv/bin/pytest tests/test_event_content.py -q`
Expected: 5 failures, `TypeError: render_description() got an unexpected keyword argument 'sections'`.

- [ ] **Step 4: Implement**

In `services/event_content.py`, add `import html` at the top and replace `render_description` with:

```python
def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _render_html(
    context: str | None, key_points: list[str], links: list[tuple[str, str]], sections: list[dict]
) -> str:
    """Digest shape (docs/event-content-standard.md → Digest): Google Calendar
    renders HTML in descriptions, so a section title can be a real link."""
    blocks: list[str] = []
    if context and context.strip():
        blocks.append(f"<p>{_esc(context.strip())}</p>")
    for section in sections:
        title = _esc(section.get("title") or "")
        url = section.get("url")
        head = f'<b><a href="{_esc(url)}">{title}</a></b>' if url else f"<b>{title}</b>"
        items = [f"<li>{_esc(p.strip())}</li>" for p in section.get("points") or [] if p and p.strip()]
        items += [
            f'<li><a href="{_esc(u)}">{_esc(lbl or u)}</a></li>'
            for u, lbl in section.get("links") or []
            if u
        ]
        blocks.append(head + (f"<ul>{''.join(items)}</ul>" if items else ""))
    points = [p.strip() for p in (key_points or []) if p and p.strip()]
    if points:
        blocks.append("<ul>" + "".join(f"<li>{_esc(p)}</li>" for p in points) + "</ul>")
    pairs = [(u, lbl) for u, lbl in (links or []) if u]
    if pairs:
        blocks.append(
            "<p>Links:<br>"
            + "<br>".join(f'<a href="{_esc(u)}">{_esc(lbl or u)}</a>' for u, lbl in pairs)
            + "</p>"
        )
    return "".join(blocks)


def render_description(
    context: str | None,
    key_points: list[str],
    links: list[tuple[str, str]],
    sections: list[dict] | None = None,
) -> str | None:
    if sections:
        return _render_html(context, key_points, links, sections)
    blocks: list[str] = []
    if context and context.strip():
        blocks.append(context.strip())
    points = [p.strip() for p in (key_points or []) if p and p.strip()]
    if points:
        blocks.append("\n".join(f"- {p}" for p in points))
    pairs = [(u, lbl) for u, lbl in (links or []) if u]
    if pairs:
        blocks.append("Links:\n" + "\n".join(f"{lbl or url}: {url}" for url, lbl in pairs))
    return "\n\n".join(blocks) if blocks else None
```

In `build_body`, add the parameter `sections: list[dict] | None = None,` after `links`, and change the description line to:

```python
    description = render_description(context, key_points or [], links or [], sections)
```

- [ ] **Step 5: Run tests**

Run: `cd ~/src/schedule && .venv/bin/pytest tests/test_event_content.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd ~/src/schedule && git add services/event_content.py tests/test_event_content.py && git commit -q -m "feat: render structured sections as HTML in event descriptions"
```

---

### Task 2: schedule-api — `sections` on create and patch, docs, PR

**Files:**
- Modify: `~/src/schedule/api/routers/events.py` (`CreateEventRequest` ~line 198, `PatchEventRequest` ~line 332, `CONTENT_FIELDS` ~line 329, `create_event` ~line 290, `patch_event` description block ~line 460)
- Modify: `~/src/schedule/docs/event-content-standard.md`
- Test: `~/src/schedule/tests/test_api_events.py`

**Interfaces:**
- Consumes: Task 1's `render_description(..., sections)` and `build_body(..., sections)`.
- Produces: `POST /events` and `PATCH /events/{id}` accept `sections: [{title, url?, points, links}]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_events.py`:

```python
def test_create_with_sections_renders_html_description(client, monkeypatch):
    captured = {}

    def fake_insert(body, *, calendar_id, send_updates, conference):
        captured["body"] = body
        return {"id": "new1", "htmlLink": "https://cal/new1"}

    monkeypatch.setattr("clients.google_calendar.insert_event", fake_insert)
    monkeypatch.setattr("api.routers.events._conflicts", lambda *a, **k: [])
    resp = client.post(
        "/events",
        headers=AUTH,
        json={
            "title": "1 task due",
            "date": "2026-09-10",
            "transparency": "transparent",
            "sections": [
                {
                    "title": "[P1] Renew passport",
                    "url": "https://app.asana.com/0/1/2",
                    "points": ["Bring DS-82"],
                    "links": [["https://drive/1", "DS-82"]],
                }
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    assert captured["body"]["description"] == (
        '<b><a href="https://app.asana.com/0/1/2">[P1] Renew passport</a></b>'
        '<ul><li>Bring DS-82</li><li><a href="https://drive/1">DS-82</a></li></ul>'
    )
    assert captured["body"]["transparency"] == "transparent"


def test_patch_with_sections_rerenders_description(client, monkeypatch):
    captured = {}

    def fake_patch(event_id, body, *, calendar_id, send_updates):
        captured["body"] = body
        return {}

    monkeypatch.setattr("clients.google_calendar.patch_event", fake_patch)
    monkeypatch.setattr("api.routers.events.imported_uid", lambda cal, eid: None)
    resp = client.patch(
        "/events/e1?calendar=primary",
        headers=AUTH,
        json={"title": "2 tasks due", "sections": [{"title": "T", "points": ["p"]}]},
    )
    assert resp.status_code == 200, resp.text
    assert captured["body"]["summary"] == "2 tasks due"
    assert captured["body"]["description"] == "<b>T</b><ul><li>p</li></ul>"
    assert resp.json()["fields"] == ["description", "summary"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/src/schedule && .venv/bin/pytest tests/test_api_events.py -q -k sections`
Expected: 2 failures with status 422 (`extra inputs are not permitted`).

- [ ] **Step 3: Implement the models and wiring**

In `api/routers/events.py`, above `CreateEventRequest` add:

```python
class Section(BaseModel):
    """One entry of a digest-shaped description — see
    docs/event-content-standard.md → Digest. `url` makes the title a link."""

    title: str = Field(min_length=1)
    url: str | None = None
    points: list[str] = []
    links: list[tuple[str, str]] = []

    model_config = {"extra": "forbid"}
```

Add `sections: list[Section] = []` to `CreateEventRequest` (after `links`) and `sections: list[Section] | None = None` to `PatchEventRequest` (after `links`).

Change `CONTENT_FIELDS = ("context", "key_points", "links")` to `CONTENT_FIELDS = ("context", "key_points", "links", "sections")`.

In `create_event`, pass `sections=[s.model_dump() for s in req.sections],` to `ec.build_body` (after `links=req.links,`).

In `patch_event`, replace the description block with:

```python
    if any(f in sent for f in CONTENT_FIELDS):
        body["description"] = ec.render_description(
            req.context,
            req.key_points or [],
            req.links or [],
            [s.model_dump() for s in (req.sections or [])],
        )
```

- [ ] **Step 4: Run the full schedule suite**

Run: `cd ~/src/schedule && .venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Document the shape**

Append to `docs/event-content-standard.md`:

```markdown
## Digest

A machine-built list event — today, the tasks service's due-day digest
(`~/src/tasks`, `docs/superpowers/specs/2026-09-03-due-day-digest-design.md`)
— passes `sections` instead of prose: a list of `{title, url, points, links}`.
When `sections` is non-empty the description renders as HTML (Google Calendar
renders it): `context` as a paragraph, then per section a bold title that is
a link when `url` is set, followed by one bullet list of its `points` and then
its `links`; top-level `key_points` and `links` follow. Everything is
escaped. Without `sections` nothing changes — plain text, as above.

`event-builder` never sends `sections`; a person-facing event is prose and
key points. `sections` exists for lists of things that each have their own
page to link to.
```

- [ ] **Step 6: Commit and open the PR**

```bash
cd ~/src/schedule && git add api/routers/events.py tests/test_api_events.py docs/event-content-standard.md && git commit -q -m "feat: structured sections on POST/PATCH /events for digest-shaped descriptions"
```

Then invoke the `/pr-open` skill from `~/src/schedule` (its CLAUDE.md requires it). Merging that PR deploys `schedule-api` via its `deploy-api.yml`. **Task 12's live check needs this deployed.**

---

### Task 3: tasks — digest models and pure routing/window/ordering

**Files:**
- Create: `models/digest.py`
- Create: `services/due_digest.py`
- Test: `tests/test_due_digest.py`

**Interfaces:**
- Produces:
  - `models.digest.DigestTask(gid, name, permalink_url, due_on, calendar_id, points, links)`
  - `models.digest.DigestEvent(day, calendar_id, title, sections, task_gids)` with `.content_hash() -> str`
  - `models.digest.Plan(creates, updates, deletes)`
  - `services.due_digest.PRIMARY = "primary"`, `WINDOW_DAYS = 30`, `LOCAL_TZ = "America/Los_Angeles"`
  - `today_local(now=None) -> date`
  - `route(task: dict, *, family_project_gid, family_calendar_id, shared_calendar_id) -> str`
  - `in_window(task: dict, today: date, days=WINDOW_DAYS) -> bool`
  - `order(tasks: list[DigestTask]) -> list[DigestTask]`
  - `title_for(n: int) -> str`
  - `build_events(tasks: list[DigestTask]) -> dict[tuple[str, str], DigestEvent]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_due_digest.py`:

```python
from datetime import date, datetime, timezone

from models.digest import DigestTask
from services import due_digest as dd

ROUTE = dict(family_project_gid="fam", family_calendar_id="cal-fam", shared_calendar_id="cal-shared")


def _task(gid="1", name="[P1] Do thing", due_on="2026-09-10", calendar_id="primary", points=None, links=None):
    return DigestTask(
        gid=gid,
        name=name,
        permalink_url=f"https://app.asana.com/0/0/{gid}",
        due_on=due_on,
        calendar_id=calendar_id,
        points=points or [],
        links=links or [],
    )


def test_today_local_uses_los_angeles():
    # 2026-09-10T03:00Z is still 2026-09-09 in Los Angeles (UTC-7).
    assert dd.today_local(datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)) == date(2026, 9, 9)


def test_route_family_project_wins_over_tag():
    task = {"memberships": [{"project": {"gid": "fam"}}], "tags": [{"name": "cheryl"}]}
    assert dd.route(task, **ROUTE) == "cal-fam"


def test_route_cheryl_tag_case_insensitive():
    task = {"memberships": [{"project": {"gid": "other"}}], "tags": [{"name": "Cheryl"}]}
    assert dd.route(task, **ROUTE) == "cal-shared"


def test_route_default_primary():
    task = {"memberships": [{"project": {"gid": "other"}}], "tags": []}
    assert dd.route(task, **ROUTE) == dd.PRIMARY


def test_route_skips_rules_whose_env_is_missing():
    task = {"memberships": [{"project": {"gid": "fam"}}], "tags": [{"name": "cheryl"}]}
    assert dd.route(task, family_project_gid="", family_calendar_id="", shared_calendar_id="") == "primary"
    assert dd.route(task, family_project_gid="", family_calendar_id="", shared_calendar_id="s") == "s"


def test_in_window_edges():
    today = date(2026, 9, 1)
    assert dd.in_window({"due_on": "2026-09-01", "completed": False}, today)
    assert dd.in_window({"due_on": "2026-10-01", "completed": False}, today)  # day 30
    assert not dd.in_window({"due_on": "2026-10-02", "completed": False}, today)  # day 31
    assert not dd.in_window({"due_on": "2026-08-31", "completed": False}, today)
    assert not dd.in_window({"due_on": None, "completed": False}, today)
    assert not dd.in_window({"due_on": "2026-09-05", "completed": True}, today)


def test_order_by_priority_prefix_then_name():
    tasks = [
        _task("a", "zeta"),
        _task("b", "[P3] beta"),
        _task("c", "[P0] Alpha"),
        _task("d", "[P0] alpha2"),
        _task("e", "Beta"),
    ]
    assert [t.gid for t in dd.order(tasks)] == ["c", "d", "b", "e", "a"]


def test_title_for_pluralizes():
    assert dd.title_for(1) == "1 task due"
    assert dd.title_for(3) == "3 tasks due"


def test_build_events_groups_by_day_and_calendar():
    tasks = [
        _task("1", "[P1] A", "2026-09-10", "primary", points=["p1"], links=[("https://d/1", "Doc")]),
        _task("2", "[P0] B", "2026-09-10", "primary"),
        _task("3", "[P2] C", "2026-09-10", "cal-fam"),
        _task("4", "[P1] D", "2026-09-11", "primary"),
    ]
    events = dd.build_events(tasks)
    assert set(events) == {("2026-09-10", "primary"), ("2026-09-10", "cal-fam"), ("2026-09-11", "primary")}
    ev = events[("2026-09-10", "primary")]
    assert ev.title == "2 tasks due"
    assert ev.task_gids == ["2", "1"]
    assert ev.sections[1] == {
        "title": "[P1] A",
        "url": "https://app.asana.com/0/0/1",
        "points": ["p1"],
        "links": [["https://d/1", "Doc"]],
    }
    assert ev.content_hash() == events[("2026-09-10", "primary")].content_hash()
    assert ev.content_hash() != events[("2026-09-11", "primary")].content_hash()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_due_digest.py -q`
Expected: `ModuleNotFoundError: No module named 'models.digest'`.

- [ ] **Step 3: Create `models/digest.py`**

```python
"""Pure types for the due-day digest. No imports from other layers.

A DigestTask is one Asana task after routing and bullet condensing; a
DigestEvent is the calendar event one (day, calendar) pair should show; a
Plan is the diff between desired events and the rows the DB says exist."""

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DigestTask:
    gid: str
    name: str
    permalink_url: str
    due_on: str  # YYYY-MM-DD
    calendar_id: str  # "primary" or a real calendar id
    points: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, label)


@dataclass
class DigestEvent:
    day: str  # YYYY-MM-DD
    calendar_id: str
    title: str
    sections: list[dict]  # schedule-api `sections` payload, JSON-ready
    task_gids: list[str]

    def content_hash(self) -> str:
        payload = json.dumps(
            {"title": self.title, "sections": self.sections}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class Plan:
    creates: list[DigestEvent] = field(default_factory=list)
    updates: list[tuple[DigestEvent, dict]] = field(default_factory=list)  # (desired, stored row)
    deletes: list[dict] = field(default_factory=list)  # stored rows
```

- [ ] **Step 4: Create `services/due_digest.py`**

```python
"""Due-day digest — pure policy: which calendar a task belongs on, which tasks
fall in the window, how a day's tasks become one event, and the diff between
desired events and stored rows. No I/O; handlers/due_digest.py orchestrates.
Design: docs/superpowers/specs/2026-09-03-due-day-digest-design.md."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models.digest import DigestEvent, DigestTask, Plan

PRIMARY = "primary"
WINDOW_DAYS = 30
LOCAL_TZ = "America/Los_Angeles"  # the primary calendar's zone (GET /calendars)

_PRIORITY_RE = re.compile(r"^\[P([0-3])\]")


def today_local(now: datetime | None = None) -> date:
    """The date in the primary calendar's zone — never the CF's UTC clock."""
    now = now or datetime.now(ZoneInfo(LOCAL_TZ))
    return now.astimezone(ZoneInfo(LOCAL_TZ)).date()


def route(
    task: dict, *, family_project_gid: str, family_calendar_id: str, shared_calendar_id: str
) -> str:
    """First match wins: Family Board membership → family calendar; a `cheryl`
    tag → shared calendar; else primary. A rule whose configuration is empty
    is skipped, so the digest still runs with a partial config."""
    if family_project_gid and family_calendar_id:
        for membership in task.get("memberships") or []:
            if ((membership.get("project") or {}).get("gid")) == family_project_gid:
                return family_calendar_id
    if shared_calendar_id:
        for tag in task.get("tags") or []:
            if (tag.get("name") or "").strip().casefold() == "cheryl":
                return shared_calendar_id
    return PRIMARY


def in_window(task: dict, today: date, days: int = WINDOW_DAYS) -> bool:
    """Open, dated, and due within [today, today + days] inclusive."""
    if task.get("completed"):
        return False
    due_on = task.get("due_on")
    if not due_on:
        return False
    due = date.fromisoformat(due_on)
    return today <= due <= today + timedelta(days=days)


def _priority_rank(name: str) -> int:
    m = _PRIORITY_RE.match(name or "")
    return int(m.group(1)) if m else 9


def order(tasks: list[DigestTask]) -> list[DigestTask]:
    return sorted(tasks, key=lambda t: (_priority_rank(t.name), t.name.casefold()))


def title_for(n: int) -> str:
    return f"{n} task due" if n == 1 else f"{n} tasks due"


def build_events(tasks: list[DigestTask]) -> dict[tuple[str, str], DigestEvent]:
    groups: dict[tuple[str, str], list[DigestTask]] = {}
    for task in tasks:
        groups.setdefault((task.due_on, task.calendar_id), []).append(task)
    events: dict[tuple[str, str], DigestEvent] = {}
    for key, members in groups.items():
        ordered = order(members)
        events[key] = DigestEvent(
            day=key[0],
            calendar_id=key[1],
            title=title_for(len(ordered)),
            sections=[
                {
                    "title": t.name,
                    "url": t.permalink_url,
                    "points": list(t.points),
                    "links": [[u, lbl] for u, lbl in t.links],
                }
                for t in ordered
            ],
            task_gids=[t.gid for t in ordered],
        )
    return events
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_due_digest.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add models/digest.py services/due_digest.py tests/test_due_digest.py && git commit -q -m "feat(digest): routing, window, ordering, and event grouping"
```

---

### Task 4: tasks — the diff (`plan`)

**Files:**
- Modify: `services/due_digest.py`
- Test: `tests/test_due_digest.py`

**Interfaces:**
- Produces: `plan(desired: dict[tuple[str, str], DigestEvent], stored: list[dict], today: date) -> Plan`. A stored row is `{"day": "YYYY-MM-DD", "calendar_id": str, "event_id": str, "content_hash": str, "task_gids": list[str]}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_due_digest.py`:

```python
def _row(day, cal="primary", event_id="e1", content_hash="h"):
    return {"day": day, "calendar_id": cal, "event_id": event_id, "content_hash": content_hash, "task_gids": []}


def test_plan_creates_updates_deletes_and_leaves_past_alone():
    today = date(2026, 9, 10)
    desired = dd.build_events([_task("1", "[P1] A", "2026-09-10"), _task("2", "[P1] B", "2026-09-12")])
    same = desired[("2026-09-12", "primary")]
    stored = [
        _row("2026-09-12", content_hash=same.content_hash(), event_id="keep"),  # unchanged
        _row("2026-09-11", event_id="gone"),  # in window, no tasks → delete
        _row("2026-09-01", event_id="past"),  # past → untouched
    ]
    p = dd.plan(desired, stored, today)
    assert [e.day for e in p.creates] == ["2026-09-10"]
    assert p.updates == []
    assert [r["event_id"] for r in p.deletes] == ["gone"]


def test_plan_updates_when_hash_differs():
    today = date(2026, 9, 10)
    desired = dd.build_events([_task("1", "[P1] A", "2026-09-10")])
    stored = [_row("2026-09-10", content_hash="stale", event_id="e9")]
    p = dd.plan(desired, stored, today)
    assert p.creates == [] and p.deletes == []
    assert p.updates[0][0].day == "2026-09-10" and p.updates[0][1]["event_id"] == "e9"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_due_digest.py -q -k plan`
Expected: `AttributeError: module 'services.due_digest' has no attribute 'plan'`.

- [ ] **Step 3: Implement**

Append to `services/due_digest.py`:

```python
def plan(desired: dict[tuple[str, str], DigestEvent], stored: list[dict], today: date) -> Plan:
    """Diff desired events against stored rows. Past days are never touched:
    the calendar keeps what was there. `day` in rows is ISO text (the repo
    normalizes it)."""
    result = Plan()
    by_key = {(str(r["day"]), r["calendar_id"]): r for r in stored}
    for key, event in desired.items():
        row = by_key.get(key)
        if row is None:
            result.creates.append(event)
        elif row["content_hash"] != event.content_hash():
            result.updates.append((event, row))
    today_iso = today.isoformat()
    for key, row in by_key.items():
        if key not in desired and key[0] >= today_iso:
            result.deletes.append(row)
    return result
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_due_digest.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/due_digest.py tests/test_due_digest.py && git commit -q -m "feat(digest): plan the create/update/delete diff against stored rows"
```

---

### Task 5: tasks — links, description text, fallback bullets, content hash

**Files:**
- Create: `services/task_bullets.py`
- Test: `tests/test_task_bullets.py`

**Interfaces:**
- Produces: `parse_links(html_notes: str) -> list[tuple[str, str]]` (Links block only, de-duplicated, max 5); `description_text(html_notes: str) -> str` (text before Actions/Source, whitespace-collapsed); `fallback_points(html_notes: str) -> list[str]`; `content_hash(name: str, html_notes: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_task_bullets.py`:

```python
from services import task_bullets as tb

NOTES = (
    "<body>Renew before the trip.\n"
    "<strong>Key points:</strong><ul><li>Expires 2026-10-01</li><li>Agency needs DS-82</li><li>Photos too</li></ul>"
    '<strong>Links:</strong><ul><li><a href="https://drive/ds82">DS-82 (filled)</a></li>'
    '<li><a href="https://drive/ds82">dup</a></li><li><a href="https://travel.state.gov/x">State Dept</a></li></ul>'
    '<strong>Actions</strong><ul><li><a href="https://hook/label?id=1">Confirmed review</a></li></ul>'
    '<strong>Source:</strong> Email<ul><li><a href="https://outlook/msg">Open in Outlook</a></li></ul></body>'
)


def test_parse_links_only_links_block_deduped():
    assert tb.parse_links(NOTES) == [
        ("https://drive/ds82", "DS-82 (filled)"),
        ("https://travel.state.gov/x", "State Dept"),
    ]


def test_parse_links_caps_at_five():
    html = "<strong>Links:</strong><ul>" + "".join(
        f'<li><a href="https://d/{i}">{i}</a></li>' for i in range(8)
    ) + "</ul>"
    assert len(tb.parse_links(html)) == 5


def test_parse_links_empty_or_bad_html():
    assert tb.parse_links("") == []
    assert tb.parse_links("<a href='https://x'>outside links</a>") == []


def test_description_text_drops_actions_and_source():
    text = tb.description_text(NOTES)
    assert text.startswith("Renew before the trip. Key points: Expires 2026-10-01")
    assert "Confirmed review" not in text
    assert "Outlook" not in text
    assert "DS-82 (filled)" in text


def test_fallback_points_prefers_key_points():
    assert tb.fallback_points(NOTES) == ["Expires 2026-10-01", "Agency needs DS-82"]


def test_fallback_points_uses_lead_context_when_no_key_points():
    html = "<body>" + "word " * 60 + "<strong>Source:</strong> Created manually</body>"
    points = tb.fallback_points(html)
    assert len(points) == 1 and points[0].endswith("…") and len(points[0]) <= 141


def test_fallback_points_empty_when_nothing():
    assert tb.fallback_points("<body><strong>Source:</strong> Created manually</body>") == []


def test_content_hash_changes_with_name_or_notes():
    a = tb.content_hash("n", "x")
    assert a == tb.content_hash("n", "x")
    assert a != tb.content_hash("n2", "x") and a != tb.content_hash("n", "y")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_task_bullets.py -q`
Expected: `ModuleNotFoundError: No module named 'services.task_bullets'`.

- [ ] **Step 3: Implement**

Create `services/task_bullets.py`:

```python
"""Per-task bullets and doc links for the due-day digest.

Links are deterministic: anchors inside the description's `Links:` block
(the block services/task_content.py renders between `Key points:` and
`Actions`). Actions are label-webhook buttons and Source is the originating
email — neither is a document. Bullets are Claude-condensed (Task 6) with a
deterministic fallback defined here. Design: D6 in
docs/superpowers/specs/2026-09-03-due-day-digest-design.md."""

import hashlib
from html.parser import HTMLParser

MAX_LINKS = 5
POINT_CHARS = 140

_STOP_HEADINGS = ("actions", "source:")


class _NotesParser(HTMLParser):
    """One pass over html_notes: text of the substance (before Actions/Source),
    the Key points list items, and the anchors inside the Links block."""

    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.key_points: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_strong = False
        self._block = ""  # "", "key_points", "links", "stop"
        self._href: str | None = None
        self._anchor: list[str] = []
        self._li: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._block == "stop":
            return
        if tag == "strong":
            self._in_strong = True
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []
        elif tag == "li":
            self._li = []

    def handle_endtag(self, tag: str) -> None:
        if self._block == "stop":
            return
        if tag == "strong":
            self._in_strong = False
        elif tag == "a":
            if self._block == "links" and self._href:
                label = " ".join("".join(self._anchor).split())
                self.links.append((self._href, label or self._href))
            self._href = None
        elif tag == "li":
            if self._block == "key_points" and self._li is not None:
                item = " ".join("".join(self._li).split())
                if item:
                    self.key_points.append(item)
            self._li = None

    def handle_data(self, data: str) -> None:
        if self._block == "stop":
            return
        if self._in_strong:
            heading = data.strip().casefold()
            if heading.startswith(_STOP_HEADINGS):
                self._block = "stop"
                return
            if heading.startswith("key points"):
                self._block = "key_points"
            elif heading.startswith("links"):
                self._block = "links"
            else:
                self._block = ""
        self.text.append(data)
        if self._href is not None:
            self._anchor.append(data)
        if self._li is not None:
            self._li.append(data)


def _parse(html_notes: str) -> _NotesParser:
    p = _NotesParser()
    try:
        p.feed(html_notes or "")
    except Exception:
        return _NotesParser()
    return p


def parse_links(html_notes: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url, label in _parse(html_notes).links:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, label))
        if len(out) == MAX_LINKS:
            break
    return out


def description_text(html_notes: str) -> str:
    return " ".join("".join(_parse(html_notes).text).split())


def _clip(text: str, limit: int = POINT_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return head.rstrip(" ,;:.") + "…"


def fallback_points(html_notes: str) -> list[str]:
    """First two key points; else the lead prose clipped; else nothing."""
    parsed = _parse(html_notes)
    if parsed.key_points:
        return [_clip(p) for p in parsed.key_points[:2]]
    lead = " ".join("".join(parsed.text).split())
    for marker in ("Key points:", "Links:"):
        if marker in lead:
            lead = lead.split(marker, 1)[0].strip()
    return [_clip(lead)] if lead else []


def content_hash(name: str, html_notes: str) -> str:
    return hashlib.sha256(f"{name}\n{html_notes or ''}".encode()).hexdigest()
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_task_bullets.py -q`
Expected: all pass. If `test_description_text_drops_actions_and_source` fails on exact prefix text, print `tb.description_text(NOTES)` and adjust only the assertion's prefix — the invariants are "starts with the context", "contains key points", "no Actions/Source text".

- [ ] **Step 5: Commit**

```bash
git add services/task_bullets.py tests/test_task_bullets.py && git commit -q -m "feat(digest): parse doc links, description text, and fallback bullets from task notes"
```

---

### Task 6: tasks — Claude-condensed bullets with cache and budget

**Files:**
- Modify: `services/task_bullets.py`
- Test: `tests/test_task_bullets.py`

**Interfaces:**
- Consumes: `clients.claude.summarize(prompt: str) -> str`.
- Produces:
  - `DIGEST_BULLET_CALLS_MAX = 40`
  - `class Budget: __init__(self, limit=DIGEST_BULLET_CALLS_MAX); remaining -> int; spend() -> None`
  - `class BulletCache(Protocol): get(gid: str, content_hash: str) -> list[str] | None; put(gid: str, content_hash: str, points: list[str]) -> None`
  - `points_for(gid: str, name: str, html_notes: str, *, cache: BulletCache, budget: Budget) -> tuple[list[str], str]` — second value is `"cached" | "ok" | "fallback" | "capped"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task_bullets.py`:

```python
import json

import clients.claude as claude


class MemCache:
    def __init__(self):
        self.store = {}

    def get(self, gid, content_hash):
        entry = self.store.get(gid)
        return entry[1] if entry and entry[0] == content_hash else None

    def put(self, gid, content_hash, points):
        self.store[gid] = (content_hash, points)


def test_points_for_cache_hit_makes_no_call(monkeypatch):
    calls = []
    monkeypatch.setattr(claude, "summarize", lambda p: calls.append(p) or "{}")
    cache = MemCache()
    cache.put("g1", tb.content_hash("n", NOTES), ["cached point"])
    points, result = tb.points_for("g1", "n", NOTES, cache=cache, budget=tb.Budget())
    assert (points, result) == (["cached point"], "cached")
    assert calls == []


def test_points_for_miss_calls_once_and_caches(monkeypatch):
    calls = []
    monkeypatch.setattr(
        claude, "summarize", lambda p: calls.append(p) or json.dumps({"points": ["a", "b", "c", "d"]})
    )
    cache = MemCache()
    budget = tb.Budget()
    points, result = tb.points_for("g1", "n", NOTES, cache=cache, budget=budget)
    assert (points, result) == (["a", "b", "c"], "ok")
    assert cache.get("g1", tb.content_hash("n", NOTES)) == ["a", "b", "c"]
    assert budget.remaining == tb.DIGEST_BULLET_CALLS_MAX - 1
    assert "Renew before the trip." in calls[0] and "Confirmed review" not in calls[0]


def test_points_for_failure_falls_back_and_does_not_cache(monkeypatch):
    def boom(p):
        raise RuntimeError("api down")

    monkeypatch.setattr(claude, "summarize", boom)
    cache = MemCache()
    points, result = tb.points_for("g1", "n", NOTES, cache=cache, budget=tb.Budget())
    assert result == "fallback"
    assert points == ["Expires 2026-10-01", "Agency needs DS-82"]
    assert cache.store == {}


def test_points_for_unparseable_json_falls_back(monkeypatch):
    monkeypatch.setattr(claude, "summarize", lambda p: "not json")
    points, result = tb.points_for("g1", "n", NOTES, cache=MemCache(), budget=tb.Budget())
    assert result == "fallback" and points


def test_points_for_capped_uses_fallback_without_calling(monkeypatch):
    calls = []
    monkeypatch.setattr(claude, "summarize", lambda p: calls.append(p) or "{}")
    points, result = tb.points_for("g1", "n", NOTES, cache=MemCache(), budget=tb.Budget(limit=0))
    assert result == "capped" and calls == []
    assert points == ["Expires 2026-10-01", "Agency needs DS-82"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_task_bullets.py -q -k points_for`
Expected: `AttributeError: module 'services.task_bullets' has no attribute 'points_for'`.

- [ ] **Step 3: Implement**

Append to `services/task_bullets.py` (add `import json`, `import logging`, `import re`, `from typing import Protocol`, and `import clients.claude as claude` to the imports; `logger = logging.getLogger(__name__)`):

```python
DIGEST_BULLET_CALLS_MAX = 40
MAX_POINTS = 3
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$")


class Budget:
    """Per-rebuild cap on Haiku calls (D6). Past the cap a task uses the
    fallback this run and heals on a later rebuild."""

    def __init__(self, limit: int = DIGEST_BULLET_CALLS_MAX) -> None:
        self.remaining = limit

    def spend(self) -> None:
        self.remaining -= 1


class BulletCache(Protocol):
    def get(self, gid: str, content_hash: str) -> list[str] | None: ...

    def put(self, gid: str, content_hash: str, points: list[str]) -> None: ...


def _prompt(name: str, text: str) -> str:
    return (
        "Condense this Asana task into 2-3 bullet points a person can act on "
        "without opening it. Be specific: names, amounts, dates, what exactly to "
        "do. Do not restate the title or the due date. Each bullet under "
        f"{POINT_CHARS} characters, plain text, no preamble.\n"
        'Return JSON only: {"points": ["point 1", "point 2"]}\n\n'
        f"Task: {name}\n\n{text[:3000]}"
    )


def _condense(name: str, html_notes: str) -> list[str]:
    raw = claude.summarize(_prompt(name, description_text(html_notes)))
    data = json.loads(_FENCE_RE.sub("", raw.strip()))
    points = [_clip(str(p)) for p in data.get("points", []) if str(p).strip()]
    if not points:
        raise ValueError("no points returned")
    return points[:MAX_POINTS]


def points_for(
    gid: str, name: str, html_notes: str, *, cache: BulletCache, budget: Budget
) -> tuple[list[str], str]:
    """(points, result) where result ∈ cached|ok|fallback|capped. A fallback
    is never cached so the next rebuild retries the model."""
    digest = content_hash(name, html_notes)
    cached = cache.get(gid, digest)
    if cached is not None:
        return cached, "cached"
    if budget.remaining <= 0:
        return fallback_points(html_notes), "capped"
    budget.spend()
    try:
        points = _condense(name, html_notes)
    except Exception:
        logger.warning("Bullet condensing failed for task gid=%s — using fallback", gid, exc_info=True)
        return fallback_points(html_notes), "fallback"
    cache.put(gid, digest, points)
    return points, "ok"
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_task_bullets.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add services/task_bullets.py tests/test_task_bullets.py && git commit -q -m "feat(digest): Haiku-condensed bullets with per-task cache and per-rebuild budget"
```

---

### Task 7: tasks — schema and repo module

**Files:**
- Modify: `repo/schema.sql`
- Create: `repo/due_digest.py`
- Test: `tests/test_repo_due_digest.py`

**Interfaces:**
- Produces (every function takes an open `conn` first):
  - `list_events(conn, *, since: date) -> list[dict]` rows with `day` as ISO text, `task_gids` as a list
  - `upsert_event(conn, *, day: str, calendar_id: str, event_id: str, content_hash: str, task_gids: list[str]) -> None`
  - `delete_event(conn, *, day: str, calendar_id: str) -> None`
  - `prune_events(conn, *, before: date) -> None`
  - `get_bullets(conn, gid: str) -> dict | None` → `{"content_hash": str, "bullets": list[str]}`
  - `put_bullets(conn, gid: str, content_hash: str, bullets: list[str]) -> None`
  - `get_state(conn) -> dict` → `{"dirty_at": datetime | None, "last_rebuilt_at": datetime | None}`
  - `mark_dirty(conn) -> None`, `mark_rebuilt(conn) -> None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_repo_due_digest.py`:

```python
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
            {"day": date(2026, 9, 10), "calendar_id": "primary", "event_id": "e", "content_hash": "h", "task_gids": '["1"]'},
            {"day": "2026-09-11", "calendar_id": "primary", "event_id": "f", "content_hash": "h", "task_gids": ["2"]},
        ]
    )
    rows = repo.list_events(conn, since=date(2026, 9, 10))
    assert rows[0]["day"] == "2026-09-10" and rows[0]["task_gids"] == ["1"]
    assert rows[1]["day"] == "2026-09-11" and rows[1]["task_gids"] == ["2"]
    query, params = conn.executed[0]
    assert "FROM due_day_events WHERE day >= %s" in query and params == (date(2026, 9, 10),)


def test_upsert_event_is_idempotent_on_key():
    conn = RowsConn()
    repo.upsert_event(conn, day="2026-09-10", calendar_id="primary", event_id="e", content_hash="h", task_gids=["1"])
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_due_digest.py -q`
Expected: `ModuleNotFoundError: No module named 'repo.due_digest'`.

- [ ] **Step 3: Add the tables**

Append to `repo/schema.sql`:

```sql
-- Due-day digest (docs/superpowers/specs/2026-09-03-due-day-digest-design.md).
-- One row per calendar event this service created for a (day, calendar):
-- REQUIRED for the digest to run — without it we cannot tell our events
-- from anything else on the calendar, so a DB outage skips the rebuild.
CREATE TABLE IF NOT EXISTS due_day_events (
    day          DATE  NOT NULL,
    calendar_id  TEXT  NOT NULL,
    event_id     TEXT  NOT NULL,
    content_hash TEXT  NOT NULL,
    task_gids    JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, calendar_id)
);

-- Haiku-condensed bullets per task, keyed on a hash of name + html_notes.
CREATE TABLE IF NOT EXISTS task_bullets (
    task_gid     TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    bullets      JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-row rebuild state: the Asana webhook sets dirty_at; POST /digest
-- rebuilds when dirty_at > last_rebuilt_at or the last rebuild is stale.
CREATE TABLE IF NOT EXISTS digest_state (
    id              BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    dirty_at        TIMESTAMPTZ,
    last_rebuilt_at TIMESTAMPTZ
);
```

- [ ] **Step 4: Create `repo/due_digest.py`**

```python
"""due_day_events / task_bullets / digest_state — the due-day digest's state.
Takes an open connection. Unlike the rest of repo/, due_day_events is NOT
best-effort: handlers/due_digest.py skips a rebuild it cannot read."""

import json
from datetime import date
from typing import Any


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return json.loads(value) if isinstance(value, str) else list(value)


def _iso_day(value: Any) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def list_events(conn: Any, *, since: date) -> list[dict]:
    rows = conn.execute(
        "SELECT day, calendar_id, event_id, content_hash, task_gids "
        "FROM due_day_events WHERE day >= %s ORDER BY day, calendar_id",
        (since,),
    ).fetchall()
    return [
        {
            "day": _iso_day(r["day"]),
            "calendar_id": r["calendar_id"],
            "event_id": r["event_id"],
            "content_hash": r["content_hash"],
            "task_gids": _as_list(r["task_gids"]),
        }
        for r in rows
    ]


def upsert_event(
    conn: Any, *, day: str, calendar_id: str, event_id: str, content_hash: str, task_gids: list[str]
) -> None:
    conn.execute(
        """
        INSERT INTO due_day_events (day, calendar_id, event_id, content_hash, task_gids)
        VALUES (%s, %s, %s, %s, %s::jsonb)
        ON CONFLICT (day, calendar_id) DO UPDATE SET
            event_id = EXCLUDED.event_id,
            content_hash = EXCLUDED.content_hash,
            task_gids = EXCLUDED.task_gids,
            updated_at = now()
        """,
        (day, calendar_id, event_id, content_hash, json.dumps(task_gids)),
    )


def delete_event(conn: Any, *, day: str, calendar_id: str) -> None:
    conn.execute(
        "DELETE FROM due_day_events WHERE day = %s AND calendar_id = %s", (day, calendar_id)
    )


def prune_events(conn: Any, *, before: date) -> None:
    conn.execute("DELETE FROM due_day_events WHERE day < %s", (before,))


def get_bullets(conn: Any, gid: str) -> dict | None:
    row = conn.execute(
        "SELECT content_hash, bullets FROM task_bullets WHERE task_gid = %s", (gid,)
    ).fetchone()
    if row is None:
        return None
    return {"content_hash": row["content_hash"], "bullets": _as_list(row["bullets"])}


def put_bullets(conn: Any, gid: str, content_hash: str, bullets: list[str]) -> None:
    conn.execute(
        """
        INSERT INTO task_bullets (task_gid, content_hash, bullets)
        VALUES (%s, %s, %s::jsonb)
        ON CONFLICT (task_gid) DO UPDATE SET
            content_hash = EXCLUDED.content_hash,
            bullets = EXCLUDED.bullets,
            updated_at = now()
        """,
        (gid, content_hash, json.dumps(bullets)),
    )


def get_state(conn: Any) -> dict:
    row = conn.execute("SELECT dirty_at, last_rebuilt_at FROM digest_state WHERE id").fetchone()
    if row is None:
        return {"dirty_at": None, "last_rebuilt_at": None}
    return {"dirty_at": row["dirty_at"], "last_rebuilt_at": row["last_rebuilt_at"]}


def mark_dirty(conn: Any) -> None:
    conn.execute(
        "INSERT INTO digest_state (id, dirty_at) VALUES (true, now()) "
        "ON CONFLICT (id) DO UPDATE SET dirty_at = now()"
    )


def mark_rebuilt(conn: Any) -> None:
    conn.execute(
        "INSERT INTO digest_state (id, last_rebuilt_at) VALUES (true, now()) "
        "ON CONFLICT (id) DO UPDATE SET last_rebuilt_at = now()"
    )
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/pytest tests/test_repo_due_digest.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add repo/schema.sql repo/due_digest.py tests/test_repo_due_digest.py && git commit -q -m "feat(digest): due_day_events, task_bullets, digest_state tables and repo"
```

---

### Task 8: tasks — `clients/schedule_api.py`

**Files:**
- Create: `clients/schedule_api.py`
- Test: `tests/test_schedule_api.py`

**Interfaces:**
- Produces:
  - `class NotFound(Exception)`
  - `create_event(*, calendar: str, day: str, title: str, sections: list[dict]) -> dict` (the `CreatedEvent` JSON: `event_id`, `calendar_id`, `html_link`)
  - `patch_event(event_id: str, *, calendar: str, title: str, sections: list[dict]) -> dict` — raises `NotFound` on 404
  - `delete_event(event_id: str, *, calendar: str) -> None` — 404 is success
  - `search_digest_events(*, calendar: str, day: str) -> list[dict]` — all-day results whose `start` begins with `day`
  - env `SCHEDULE_API_URL`, `SCHEDULE_API_TOKEN`; `TIMEOUT = 30`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_schedule_api.py`:

```python
import httpx
import pytest

import clients.schedule_api as sapi


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SCHEDULE_API_URL", "https://sched.example")
    monkeypatch.setenv("SCHEDULE_API_TOKEN", "tok")


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(sapi, "_client", lambda: httpx.Client(transport=transport))


def test_create_event_posts_all_day_transparent(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = request.read()
        return httpx.Response(201, json={"event_id": "e1", "calendar_id": "primary", "html_link": "h"})

    _mock(monkeypatch, handler)
    out = sapi.create_event(calendar="primary", day="2026-09-10", title="1 task due", sections=[{"title": "T"}])
    assert out["event_id"] == "e1"
    assert seen["url"] == "https://sched.example/events" and seen["auth"] == "Bearer tok"
    body = httpx.Request("POST", "https://x", content=seen["json"]).read()
    import json

    payload = json.loads(body)
    assert payload == {
        "calendar": "primary",
        "date": "2026-09-10",
        "title": "1 task due",
        "sections": [{"title": "T"}],
        "transparency": "transparent",
    }


def test_patch_event_404_raises_not_found(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"detail": "gone"}))
    with pytest.raises(sapi.NotFound):
        sapi.patch_event("e1", calendar="c", title="t", sections=[])


def test_patch_event_sends_calendar_query(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["method"] = request.method
        return httpx.Response(200, json={"status": "updated", "event_id": "e1", "calendar_id": "c", "fields": []})

    _mock(monkeypatch, handler)
    sapi.patch_event("e1", calendar="c", title="t", sections=[])
    assert seen["method"] == "PATCH" and seen["url"] == "https://sched.example/events/e1?calendar=c"


def test_delete_event_treats_404_as_success(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(404, json={"detail": "gone"}))
    sapi.delete_event("e1", calendar="c")  # no raise


def test_other_errors_raise(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(httpx.HTTPStatusError):
        sapi.delete_event("e1", calendar="c")


def test_search_digest_events_filters_to_day(monkeypatch):
    seen = {}

    def handler(request):
        import json

        seen["json"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "results": [
                    {"event_id": "a", "start": "2026-09-10", "all_day": True, "title": "2 tasks due"},
                    {"event_id": "b", "start": "2026-09-11", "all_day": True, "title": "1 task due"},
                ],
                "window": {"time_min": "", "time_max": ""},
                "calendars_searched": ["c"],
            },
        )

    _mock(monkeypatch, handler)
    out = sapi.search_digest_events(calendar="c", day="2026-09-10")
    assert [r["event_id"] for r in out] == ["a"]
    assert seen["json"]["calendar"] == "c" and seen["json"]["all_day"] is True
    assert seen["json"]["time_min"].startswith("2026-09-09") and seen["json"]["time_max"].startswith("2026-09-11")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule_api.py -q`
Expected: `ModuleNotFoundError: No module named 'clients.schedule_api'`.

- [ ] **Step 3: Implement**

Create `clients/schedule_api.py`:

```python
"""Client for the schedule-api Cloud Run service — the calendar gateway.

This repo never talks to Google Calendar directly (schedule owns every
calendar write, its dedup and routing invariants included). The due-day
digest (handlers/due_digest.py) is the first consumer: all-day events with
a `sections` body — see ~/src/schedule/docs/event-content-standard.md."""

import os

import httpx

TIMEOUT = 30


class NotFound(Exception):
    """The event is gone on the calendar (deleted by hand)."""


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=os.environ.get("SCHEDULE_API_URL", ""),
        headers={"Authorization": f"Bearer {os.environ.get('SCHEDULE_API_TOKEN', '')}"},
        timeout=TIMEOUT,
    )


def create_event(*, calendar: str, day: str, title: str, sections: list[dict]) -> dict:
    payload = {
        "calendar": calendar,
        "date": day,
        "title": title,
        "sections": sections,
        "transparency": "transparent",
    }
    with _client() as client:
        resp = client.post("/events", json=payload)
    resp.raise_for_status()
    return resp.json()


def patch_event(event_id: str, *, calendar: str, title: str, sections: list[dict]) -> dict:
    with _client() as client:
        resp = client.patch(
            f"/events/{event_id}",
            params={"calendar": calendar},
            json={"title": title, "sections": sections},
        )
    if resp.status_code == 404:
        raise NotFound(event_id)
    resp.raise_for_status()
    return resp.json()


def delete_event(event_id: str, *, calendar: str) -> None:
    with _client() as client:
        resp = client.delete(f"/events/{event_id}", params={"calendar": calendar})
    if resp.status_code == 404:
        return
    resp.raise_for_status()


def search_digest_events(*, calendar: str, day: str) -> list[dict]:
    """All-day events on `calendar` that start on `day`. The window is padded
    a day each side because all-day events are matched by overlap in UTC."""
    from datetime import date, timedelta

    d = date.fromisoformat(day)
    payload = {
        "query": "tasks due",
        "calendar": calendar,
        "time_min": f"{(d - timedelta(days=1)).isoformat()}T00:00:00Z",
        "time_max": f"{(d + timedelta(days=1)).isoformat()}T23:59:59Z",
        "all_day": True,
        "limit": 50,
    }
    with _client() as client:
        resp = client.post("/search", json=payload)
    resp.raise_for_status()
    return [r for r in resp.json().get("results", []) if (r.get("start") or "").startswith(day)]
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_schedule_api.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add clients/schedule_api.py tests/test_schedule_api.py && git commit -q -m "feat(digest): schedule-api client for all-day digest events"
```

---

### Task 9: tasks — Asana listing opt-fields and OTel counters

**Files:**
- Modify: `clients/asana.py:20-33` (constants), `:328-346` (`list_project_tasks`, `list_my_tasks`)
- Modify: `clients/otel.py` (module-level no-op counters ~line 24-38; `global` line ~47-50; counter creation ~line 100-125)
- Test: `tests/test_asana_client.py`, `tests/test_otel.py`

**Interfaces:**
- Produces: `asana.DIGEST_OPT_FIELDS`; `list_project_tasks(project_gid, *, only_open=False, opt_fields=SEARCH_OPT_FIELDS)`; `list_my_tasks(*, only_open=False, opt_fields=SEARCH_OPT_FIELDS)`; counters `otel.digest_rebuilds`, `otel.digest_events`, `otel.digest_bullet_calls`, `otel.digest_errors`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_asana_client.py` (it already imports `clients.asana as asana` and uses `monkeypatch` on `asana._request`; follow the existing `_paginate` test style — if none exists, this stub is complete):

```python
def test_list_project_tasks_accepts_opt_fields_override(monkeypatch):
    seen = {}

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [], "next_page": None}

    def fake_request(method, path, *, operation, params=None, **kw):
        seen["params"] = params
        return Resp()

    monkeypatch.setattr(asana, "_request", fake_request)
    asana.list_project_tasks("p1", only_open=True, opt_fields=asana.DIGEST_OPT_FIELDS)
    assert seen["params"]["opt_fields"] == asana.DIGEST_OPT_FIELDS
    assert "tags.name" in asana.DIGEST_OPT_FIELDS and "html_notes" in asana.DIGEST_OPT_FIELDS
    assert seen["params"]["completed_since"] == "now"
```

Append to `tests/test_otel.py`:

```python
def test_digest_counters_exist():
    import clients.otel as otel

    for name in ("digest_rebuilds", "digest_events", "digest_bullet_calls", "digest_errors"):
        getattr(otel, name).add(1, {"outcome": "test"})  # no-op meter never raises
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_asana_client.py tests/test_otel.py -q -k "opt_fields or digest_counters"`
Expected: 2 failures (`TypeError: unexpected keyword argument 'opt_fields'`, `AttributeError: digest_rebuilds`).

- [ ] **Step 3: Implement in `clients/asana.py`**

After `DETAIL_OPT_FIELDS` add:

```python
# Due-day digest listing: tags (routing), html_notes (bullets + doc links),
# project membership (routing), modified_at (diagnostics).
DIGEST_OPT_FIELDS = (
    "name,html_notes,due_on,completed,permalink_url,modified_at,tags.name,"
    "memberships.project.gid,memberships.project.name,parent.gid"
)
```

Change the two list functions:

```python
def list_project_tasks(
    project_gid: str, *, only_open: bool = False, opt_fields: str = SEARCH_OPT_FIELDS
) -> list[dict]:
    params: dict = {"project": project_gid, "opt_fields": opt_fields}
    if only_open:
        params["completed_since"] = "now"
    return _paginate("/tasks", params, operation="list_project_tasks")


def list_my_tasks(*, only_open: bool = False, opt_fields: str = SEARCH_OPT_FIELDS) -> list[dict]:
    """Workspace tasks assigned to the token's user — catches My-Tasks items
    that are in no project. Overlaps with project listings; callers de-dupe."""
    params: dict = {
        "assignee": "me",
        "workspace": get_workspace_gid(),
        "opt_fields": opt_fields,
    }
    if only_open:
        params["completed_since"] = "now"
    return _paginate("/tasks", params, operation="list_my_tasks")
```

- [ ] **Step 4: Implement in `clients/otel.py`**

Add after the `recurrences` no-op line:

```python
digest_rebuilds: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
digest_events: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
digest_bullet_calls: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
digest_errors: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

In `setup_telemetry`, extend the `global` statement with `digest_rebuilds, digest_events, digest_bullet_calls, digest_errors` and after the `recurrences = meter.create_counter(...)` block add:

```python
    digest_rebuilds = meter.create_counter(
        "asana.digest.rebuilds",
        description="Due-day digest rebuilds by outcome (ok|partial|skipped|db_unavailable|error)",
    )
    digest_events = meter.create_counter(
        "asana.digest.events", description="Digest calendar events by op (create|update|delete|adopt)"
    )
    digest_bullet_calls = meter.create_counter(
        "asana.digest.bullet_calls", description="Bullet condensing by result (ok|cached|fallback|capped)"
    )
    digest_errors = meter.create_counter(
        "asana.digest.errors", description="Digest failures by stage (list|bullets|calendar)"
    )
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add clients/asana.py clients/otel.py tests/test_asana_client.py tests/test_otel.py && git commit -q -m "feat(digest): digest opt-fields on Asana listings and digest OTel counters"
```

---

### Task 10: tasks — `handlers/due_digest.py`

**Files:**
- Create: `handlers/due_digest.py`
- Test: `tests/test_due_digest_handler.py`

**Interfaces:**
- Consumes: everything from Tasks 3–9.
- Produces: `run(*, force: bool = False) -> dict` returning `{"outcome": "skipped"}` or `{"outcome": "ok"|"partial"|"db_unavailable"|"error", "tasks": int, "created": int, "updated": int, "deleted": int, "adopted": int, "bullet_calls": int, "errors": int}`; `should_rebuild(state: dict, now: datetime, force: bool) -> bool`; `STALE_AFTER = timedelta(minutes=60)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_due_digest_handler.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

import clients.asana as asana
import clients.schedule_api as sapi
from handlers import due_digest as h
from repo import due_digest as repo
from services import task_bullets as tb
from tests.test_repo_due_digest import RowsConn

NOW = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)  # 08:00 Los Angeles → today = 2026-09-10


def test_should_rebuild_matrix():
    rebuilt = NOW - timedelta(minutes=5)
    assert h.should_rebuild({"dirty_at": NOW, "last_rebuilt_at": rebuilt}, NOW, False)
    assert not h.should_rebuild({"dirty_at": rebuilt - timedelta(minutes=1), "last_rebuilt_at": rebuilt}, NOW, False)
    assert h.should_rebuild({"dirty_at": None, "last_rebuilt_at": None}, NOW, False)
    assert h.should_rebuild({"dirty_at": None, "last_rebuilt_at": NOW - timedelta(minutes=61)}, NOW, False)
    assert not h.should_rebuild({"dirty_at": None, "last_rebuilt_at": NOW - timedelta(minutes=59)}, NOW, False)
    assert h.should_rebuild({"dirty_at": None, "last_rebuilt_at": rebuilt}, NOW, True)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("SCHEDULE_API_URL", "https://s")
    monkeypatch.setenv("SCHEDULE_API_TOKEN", "t")
    monkeypatch.setenv("ASANA_PROJECT_FAMILY_GID", "fam")
    monkeypatch.setenv("CALENDAR_FAMILY_ID", "cal-fam")
    monkeypatch.setenv("CALENDAR_SHARED_ID", "cal-shared")
    monkeypatch.setattr(h, "_now", lambda: NOW)


def _asana(monkeypatch, tasks):
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p1", "name": "Work"}, {"gid": "fam", "name": "Family Board"}])
    monkeypatch.setattr(asana, "list_project_tasks", lambda gid, only_open=False, opt_fields=None: [t for t in tasks if t["_project"] == gid])
    monkeypatch.setattr(asana, "list_my_tasks", lambda only_open=False, opt_fields=None: list(tasks))


def _task(gid, name, due_on, project="p1", tags=(), notes="<body>x</body>"):
    return {
        "gid": gid,
        "name": name,
        "due_on": due_on,
        "completed": False,
        "permalink_url": f"https://app.asana.com/0/0/{gid}",
        "html_notes": notes,
        "tags": [{"name": t} for t in tags],
        "memberships": [{"project": {"gid": project}}],
        "_project": project,
    }


class Store:
    """In-memory stand-in for repo.due_digest, patched function by function."""

    def __init__(self, rows=None, state=None):
        self.rows = {(r["day"], r["calendar_id"]): r for r in (rows or [])}
        self.state = state or {"dirty_at": NOW, "last_rebuilt_at": None}
        self.bullets = {}
        self.rebuilt = 0

    def patch(self, monkeypatch):
        monkeypatch.setattr(h, "get_conn", lambda: RowsConn())
        monkeypatch.setattr(repo, "get_state", lambda conn: self.state)
        monkeypatch.setattr(repo, "list_events", lambda conn, since: list(self.rows.values()))
        monkeypatch.setattr(repo, "upsert_event", lambda conn, **kw: self.rows.__setitem__((kw["day"], kw["calendar_id"]), kw))
        monkeypatch.setattr(repo, "delete_event", lambda conn, day, calendar_id: self.rows.pop((day, calendar_id), None))
        monkeypatch.setattr(repo, "prune_events", lambda conn, before: None)
        monkeypatch.setattr(repo, "get_bullets", lambda conn, gid: self.bullets.get(gid))
        monkeypatch.setattr(repo, "put_bullets", lambda conn, gid, ch, b: self.bullets.__setitem__(gid, {"content_hash": ch, "bullets": b}))

        def mark():
            self.rebuilt += 1

        monkeypatch.setattr(repo, "mark_rebuilt", lambda conn: mark())


class Cal:
    def __init__(self, patch_404=False, search_hits=None):
        self.created, self.patched, self.deleted = [], [], []
        self.patch_404 = patch_404
        self.search_hits = search_hits or []
        self.n = 0

    def patch(self, monkeypatch):
        def create(**kw):
            self.n += 1
            self.created.append(kw)
            return {"event_id": f"new{self.n}", "calendar_id": kw["calendar"]}

        def patch_event(event_id, **kw):
            if self.patch_404:
                raise sapi.NotFound(event_id)
            self.patched.append((event_id, kw))
            return {}

        monkeypatch.setattr(sapi, "create_event", create)
        monkeypatch.setattr(sapi, "patch_event", patch_event)
        monkeypatch.setattr(sapi, "delete_event", lambda event_id, **kw: self.deleted.append(event_id))
        monkeypatch.setattr(sapi, "search_digest_events", lambda **kw: list(self.search_hits))


def test_skipped_when_clean_and_fresh(env, monkeypatch):
    store = Store(state={"dirty_at": None, "last_rebuilt_at": NOW - timedelta(minutes=5)})
    store.patch(monkeypatch)
    assert h.run() == {"outcome": "skipped"}


def test_db_unavailable_skips(env, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(h, "get_conn", boom)
    assert h.run()["outcome"] == "db_unavailable"


def test_rebuild_creates_routed_events_and_records_rows(env, monkeypatch):
    _asana(
        monkeypatch,
        [
            _task("1", "[P1] Work thing", "2026-09-10"),
            _task("2", "[P0] Family thing", "2026-09-10", project="fam"),
            _task("3", "[P2] Shared thing", "2026-09-12", tags=("cheryl",)),
            _task("4", "[P1] Far away", "2026-12-01"),
        ],
    )
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "ok"))
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)

    out = h.run()
    assert out["outcome"] == "ok" and out["tasks"] == 3 and out["created"] == 3
    calendars = sorted(c["calendar"] for c in cal.created)
    assert calendars == ["cal-fam", "cal-shared", "primary"]
    assert set(store.rows) == {("2026-09-10", "primary"), ("2026-09-10", "cal-fam"), ("2026-09-12", "cal-shared")}
    assert store.rows[("2026-09-10", "primary")]["event_id"].startswith("new")
    assert store.rebuilt == 1


def test_rebuild_updates_deletes_and_recreates_on_404(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] Work thing", "2026-09-10")])
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "cached"))
    store = Store(
        rows=[
            {"day": "2026-09-10", "calendar_id": "primary", "event_id": "old", "content_hash": "stale", "task_gids": []},
            {"day": "2026-09-11", "calendar_id": "primary", "event_id": "gone", "content_hash": "x", "task_gids": []},
        ]
    )
    store.patch(monkeypatch)
    cal = Cal(patch_404=True)
    cal.patch(monkeypatch)

    out = h.run()
    assert out["outcome"] == "ok"
    assert out["updated"] == 0 and out["created"] == 1 and out["deleted"] == 1
    assert cal.deleted == ["gone"]
    assert store.rows[("2026-09-10", "primary")]["event_id"] == "new1"
    assert ("2026-09-11", "primary") not in store.rows


def test_rebuild_adopts_existing_digest_event_instead_of_creating(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] Work thing", "2026-09-10")])
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: (["pt"], "cached"))
    store = Store()
    store.patch(monkeypatch)
    cal = Cal(search_hits=[{"event_id": "found", "title": "2 tasks due", "start": "2026-09-10"}])
    cal.patch(monkeypatch)

    out = h.run()
    assert out["adopted"] == 1 and out["created"] == 0
    assert cal.created == [] and cal.patched[0][0] == "found"
    assert store.rows[("2026-09-10", "primary")]["event_id"] == "found"


def test_calendar_error_is_partial_and_continues(env, monkeypatch):
    _asana(monkeypatch, [_task("1", "[P1] A", "2026-09-10"), _task("2", "[P1] B", "2026-09-11")])
    monkeypatch.setattr(tb, "points_for", lambda gid, name, notes, cache, budget: ([], "cached"))
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)
    calls = {"n": 0}

    def flaky(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("500")
        return {"event_id": "ok1", "calendar_id": kw["calendar"]}

    monkeypatch.setattr(sapi, "create_event", flaky)
    out = h.run()
    assert out["outcome"] == "partial" and out["created"] == 1 and out["errors"] == 1
    assert store.rebuilt == 1


def test_listing_error_aborts_without_touching_calendar(env, monkeypatch):
    def boom():
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "list_projects", boom)
    store = Store()
    store.patch(monkeypatch)
    cal = Cal()
    cal.patch(monkeypatch)
    out = h.run()
    assert out["outcome"] == "error" and cal.created == [] and store.rebuilt == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_due_digest_handler.py -q`
Expected: `ModuleNotFoundError: No module named 'handlers.due_digest'`.

- [ ] **Step 3: Implement**

Create `handlers/due_digest.py`:

```python
"""Due-day digest rebuild: POST /digest on the webhook CF, ticked by Cloud
Scheduler every 10 minutes. Decides whether a rebuild is due, lists Asana,
routes and condenses each task, diffs against due_day_events, and applies
the diff through schedule-api. Called only from main.py.
Design: docs/superpowers/specs/2026-09-03-due-day-digest-design.md."""

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import clients.asana as asana
import clients.otel as otel
import clients.schedule_api as sapi
from clients.db import get_conn
from models.digest import DigestEvent, DigestTask
from repo import due_digest as repo
from services import due_digest as dd
from services import task_bullets as tb

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=60)
PRUNE_AFTER = timedelta(days=90)
_DIGEST_TITLE_RE = re.compile(r"^\d+ tasks? due$")
_LIST_WORKERS = 4


def _now() -> datetime:
    return datetime.now(timezone.utc)


def should_rebuild(state: dict, now: datetime, force: bool) -> bool:
    if force:
        return True
    last = state.get("last_rebuilt_at")
    if last is None or now - last > STALE_AFTER:
        return True
    dirty = state.get("dirty_at")
    return dirty is not None and dirty > last


class _RepoCache:
    """tb.BulletCache backed by task_bullets; one connection per rebuild."""

    def __init__(self, conn) -> None:
        self._conn = conn

    def get(self, gid: str, content_hash: str) -> list[str] | None:
        row = repo.get_bullets(self._conn, gid)
        return row["bullets"] if row and row["content_hash"] == content_hash else None

    def put(self, gid: str, content_hash: str, points: list[str]) -> None:
        repo.put_bullets(self._conn, gid, content_hash, points)


def _list_candidates() -> list[dict]:
    projects = asana.list_projects()
    with ThreadPoolExecutor(max_workers=_LIST_WORKERS) as pool:
        per_project = list(
            pool.map(
                lambda p: asana.list_project_tasks(
                    p["gid"], only_open=True, opt_fields=asana.DIGEST_OPT_FIELDS
                ),
                projects,
            )
        )
    raw = [t for batch in per_project for t in batch] + asana.list_my_tasks(
        only_open=True, opt_fields=asana.DIGEST_OPT_FIELDS
    )
    seen: set[str] = set()
    out: list[dict] = []
    for t in raw:
        if t["gid"] in seen:
            continue
        seen.add(t["gid"])
        out.append(t)
    return out


def _routing() -> dict:
    cfg = {
        "family_project_gid": os.environ.get("ASANA_PROJECT_FAMILY_GID", ""),
        "family_calendar_id": os.environ.get("CALENDAR_FAMILY_ID", ""),
        "shared_calendar_id": os.environ.get("CALENDAR_SHARED_ID", ""),
    }
    for key, value in cfg.items():
        if not value:
            logger.warning("Digest routing: %s unset — that rule is skipped", key.upper())
    return cfg


def _digest_tasks(candidates: list[dict], today: date, conn, counts: dict) -> list[DigestTask]:
    routing = _routing()
    cache = _RepoCache(conn)
    budget = tb.Budget()
    tasks: list[DigestTask] = []
    for t in candidates:
        if not dd.in_window(t, today):
            continue
        notes = t.get("html_notes") or ""
        points, result = tb.points_for(t["gid"], t["name"], notes, cache=cache, budget=budget)
        otel.digest_bullet_calls.add(1, {"result": result})
        if result in ("ok", "fallback"):
            counts["bullet_calls"] += 1
        tasks.append(
            DigestTask(
                gid=t["gid"],
                name=t["name"],
                permalink_url=t.get("permalink_url") or "",
                due_on=t["due_on"],
                calendar_id=dd.route(t, **routing),
                points=points,
                links=tb.parse_links(notes),
            )
        )
    return tasks


def _adopt(event: DigestEvent) -> str | None:
    """An existing digest event on that day/calendar (lost row) — reuse it."""
    for hit in sapi.search_digest_events(calendar=event.calendar_id, day=event.day):
        if _DIGEST_TITLE_RE.match(hit.get("title") or ""):
            return hit["event_id"]
    return None


def _create(event: DigestEvent, conn, counts: dict) -> None:
    event_id = _adopt(event)
    if event_id:
        sapi.patch_event(event_id, calendar=event.calendar_id, title=event.title, sections=event.sections)
        counts["adopted"] += 1
        otel.digest_events.add(1, {"op": "adopt"})
    else:
        created = sapi.create_event(
            calendar=event.calendar_id, day=event.day, title=event.title, sections=event.sections
        )
        event_id = created["event_id"]
        counts["created"] += 1
        otel.digest_events.add(1, {"op": "create"})
    repo.upsert_event(
        conn,
        day=event.day,
        calendar_id=event.calendar_id,
        event_id=event_id,
        content_hash=event.content_hash(),
        task_gids=event.task_gids,
    )


def _update(event: DigestEvent, row: dict, conn, counts: dict) -> None:
    try:
        sapi.patch_event(row["event_id"], calendar=event.calendar_id, title=event.title, sections=event.sections)
    except sapi.NotFound:
        logger.info("Digest event %s gone from calendar — recreating", row["event_id"])
        repo.delete_event(conn, day=event.day, calendar_id=event.calendar_id)
        _create(event, conn, counts)
        return
    counts["updated"] += 1
    otel.digest_events.add(1, {"op": "update"})
    repo.upsert_event(
        conn,
        day=event.day,
        calendar_id=event.calendar_id,
        event_id=row["event_id"],
        content_hash=event.content_hash(),
        task_gids=event.task_gids,
    )


def _delete(row: dict, conn, counts: dict) -> None:
    sapi.delete_event(row["event_id"], calendar=row["calendar_id"])
    repo.delete_event(conn, day=row["day"], calendar_id=row["calendar_id"])
    counts["deleted"] += 1
    otel.digest_events.add(1, {"op": "delete"})


def _apply(plan, conn, counts: dict) -> None:
    for event in plan.creates:
        try:
            _create(event, conn, counts)
        except Exception:
            counts["errors"] += 1
            otel.digest_errors.add(1, {"stage": "calendar"})
            logger.exception("Digest create failed for %s/%s", event.day, event.calendar_id)
    for event, row in plan.updates:
        try:
            _update(event, row, conn, counts)
        except Exception:
            counts["errors"] += 1
            otel.digest_errors.add(1, {"stage": "calendar"})
            logger.exception("Digest update failed for %s/%s", event.day, event.calendar_id)
    for row in plan.deletes:
        try:
            _delete(row, conn, counts)
        except Exception:
            counts["errors"] += 1
            otel.digest_errors.add(1, {"stage": "calendar"})
            logger.exception("Digest delete failed for %s/%s", row["day"], row["calendar_id"])


def run(*, force: bool = False) -> dict:
    now = _now()
    try:
        with get_conn() as conn:
            state = repo.get_state(conn)
    except Exception:
        logger.exception("Digest: DB unavailable — skipping rebuild")
        otel.digest_rebuilds.add(1, {"outcome": "db_unavailable"})
        return {"outcome": "db_unavailable"}
    if not should_rebuild(state, now, force):
        otel.digest_rebuilds.add(1, {"outcome": "skipped"})
        return {"outcome": "skipped"}
    if not (os.environ.get("SCHEDULE_API_URL") and os.environ.get("SCHEDULE_API_TOKEN")):
        logger.error("SCHEDULE_API_URL / SCHEDULE_API_TOKEN unset — digest cannot run")
        otel.digest_rebuilds.add(1, {"outcome": "error"})
        return {"outcome": "error"}

    today = dd.today_local(now)
    counts = {
        "tasks": 0, "created": 0, "updated": 0, "deleted": 0, "adopted": 0,
        "bullet_calls": 0, "errors": 0,
    }
    with otel.get_tracer().start_as_current_span("digest.rebuild") as span:
        try:
            candidates = _list_candidates()
        except Exception:
            logger.exception("Digest: Asana listing failed — nothing changed")
            otel.digest_errors.add(1, {"stage": "list"})
            otel.digest_rebuilds.add(1, {"outcome": "error"})
            return {"outcome": "error", **counts}

        try:
            with get_conn() as conn:
                tasks = _digest_tasks(candidates, today, conn, counts)
                counts["tasks"] = len(tasks)
                desired = dd.build_events(tasks)
                stored = repo.list_events(conn, since=today)
                plan = dd.plan(desired, stored, today)
                _apply(plan, conn, counts)
                repo.prune_events(conn, before=today - PRUNE_AFTER)
                repo.mark_rebuilt(conn)
        except Exception:
            logger.exception("Digest: rebuild failed")
            otel.digest_rebuilds.add(1, {"outcome": "error"})
            return {"outcome": "error", **counts}

        outcome = "partial" if counts["errors"] else "ok"
        span.set_attribute("digest.today", today.isoformat())
        for key, value in counts.items():
            span.set_attribute(f"digest.{key}", value)
    otel.digest_rebuilds.add(1, {"outcome": outcome})
    logger.info("Digest rebuild %s: %s", outcome, counts)
    return {"outcome": outcome, **counts}
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/pytest tests/test_due_digest_handler.py -q`
Expected: all pass. Note for `test_rebuild_updates_deletes_and_recreates_on_404`: the 404 path deletes the row then `_create` → search returns no hits → create → `new1`.

- [ ] **Step 5: Commit**

```bash
git add handlers/due_digest.py tests/test_due_digest_handler.py && git commit -q -m "feat(digest): rebuild handler — decision, listing, bullets, diff, apply"
```

---

### Task 11: tasks — `POST /digest` route and webhook dirty flag

**Files:**
- Modify: `main.py:80-90` (webhook routes), docstring env list
- Modify: `handlers/asana_webhook.py` (`receive`)
- Test: `tests/test_main.py`, `tests/test_asana_webhook.py`

**Interfaces:**
- Consumes: `handlers.due_digest.run(force=...)`, `services.escalation.is_authorized`, `repo.due_digest.mark_dirty`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main.py`:

```python
def test_webhook_digest_route(monkeypatch):
    from handlers import due_digest
    from services import escalation

    monkeypatch.setenv("ASANA_ESCALATE_TOKEN", "tok")
    seen = {}
    monkeypatch.setattr(due_digest, "run", lambda force=False: seen.update(force=force) or {"outcome": "ok"})
    result, status = main.webhook(
        Req(path="/digest", headers={"Authorization": "Bearer tok"}, body=b'{"force": true}')
    )
    assert status == 200 and result == {"outcome": "ok"} and seen["force"] is True


def test_webhook_digest_rejects_bad_auth(monkeypatch):
    monkeypatch.setenv("ASANA_ESCALATE_TOKEN", "tok")
    _, status = main.webhook(Req(path="/digest", headers={"Authorization": "Bearer wrong"}))
    assert status == 401
```

Append to `tests/test_asana_webhook.py`:

```python
def test_relevant_events_mark_digest_dirty(monkeypatch):
    _capture(monkeypatch)
    marks = []
    monkeypatch.setattr(asana_webhook, "_mark_digest_dirty", lambda: marks.append(1))
    body, sig = _signed(
        [{"action": "changed", "resource": {"gid": "t1", "resource_type": "task"}, "change": {"field": "due_on"}}]
    )
    asana_webhook.receive(body, sig)
    assert marks == [1]


def test_irrelevant_events_do_not_mark_dirty(monkeypatch):
    _capture(monkeypatch)
    marks = []
    monkeypatch.setattr(asana_webhook, "_mark_digest_dirty", lambda: marks.append(1))
    body, sig = _signed([{"action": "changed", "resource": {"gid": "s1", "resource_type": "story"}}])
    asana_webhook.receive(body, sig)
    assert marks == []


def test_dirty_flag_db_failure_does_not_fail_delivery(monkeypatch):
    _capture(monkeypatch)

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(asana_webhook, "get_conn", boom)
    body, sig = _signed([{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_main.py tests/test_asana_webhook.py -q -k "digest or dirty"`
Expected: failures — `/digest` returns 405 (falls through to `request.method != "POST"`… actually returns the webhook path and 401 from signature), `AttributeError: _mark_digest_dirty`.

- [ ] **Step 3: Implement the route in `main.py`**

Add `from handlers import asana_webhook, due_digest, label_applied, task_create` (replace the existing handlers import) and, immediately after the `/escalate` block:

```python
        if request.path == "/digest" and request.method == "POST":
            if not escalation.is_authorized(request.headers.get("Authorization")):
                return "", 401
            try:
                body = json.loads(request.get_data() or b"{}") or {}
            except ValueError:
                body = {}
            return due_digest.run(force=bool(body.get("force"))), 200
```

Update the module docstring: `ASANA_ESCALATE_TOKEN — bearer token for POST /escalate and POST /digest (webhook CF)` and add `SCHEDULE_API_URL / SCHEDULE_API_TOKEN — schedule-api, for the due-day digest` and `ASANA_PROJECT_FAMILY_GID / CALENDAR_FAMILY_ID / CALENDAR_SHARED_ID — digest routing`.

- [ ] **Step 4: Implement the dirty flag in `handlers/asana_webhook.py`**

Add imports `from clients.db import get_conn` and `from repo import due_digest as repo_due_digest`. Add:

```python
def _mark_digest_dirty() -> None:
    """Best-effort: the 10-minute /digest tick also rebuilds hourly, so a
    lost flag delays the digest, never the webhook."""
    try:
        with get_conn() as conn:
            repo_due_digest.mark_dirty(conn)
    except Exception:
        logger.warning("Digest dirty flag write failed — hourly rebuild will catch up", exc_info=True)
```

In `receive`, add `digest_relevant = False` before the loop, set `digest_relevant = True` inside each of the three task branches (`completed`, `deleted/removed`, `added/changed`), and after the delete-wins block add:

```python
    if digest_relevant:
        _mark_digest_dirty()
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass. Existing webhook tests must still pass — they don't stub `_mark_digest_dirty`, so it hits `get_conn()` with no env; `_direct_conn` raises `KeyError` on `POSTGRES_USER`, which the try/except swallows. If any existing test asserts on log output, stub `_mark_digest_dirty` there.

- [ ] **Step 6: Commit**

```bash
git add main.py handlers/asana_webhook.py tests/test_main.py tests/test_asana_webhook.py && git commit -q -m "feat(digest): POST /digest route and webhook-driven dirty flag"
```

---

### Task 12: tasks — terraform, CI variables, local env, docs, smoke script

**Files:**
- Modify: `terraform/variables.tf`, `terraform/secrets.tf:11-20`, `terraform/iam.tf`, `terraform/cloud_functions.tf` (common_env ~line 4-17; webhook CF block ~line 176-240), `terraform/scheduler.tf`, `terraform/terraform.tfvars.example`
- Modify: `.github/workflows/deploy.yml:40-56`
- Modify: `scripts/fetch-env.sh`
- Create: `scripts/test-digest.py`
- Modify: `CLAUDE.md`, `docs/asana-webhook-setup.md`

- [ ] **Step 1: Terraform variables**

Append to `terraform/variables.tf`:

```hcl
variable "schedule_api_url" {
  description = "schedule-api Cloud Run URL (calendar gateway for clients/schedule_api.py)"
  type        = string
  default     = "https://schedule-api.drolet.cloud"
}

variable "asana_project_family_gid" {
  description = "Asana project GID of the Family Board — its tasks' due-day digest goes to the Family calendar. Empty disables the rule."
  type        = string
  default     = ""
}

variable "calendar_family_id" {
  description = "Google Calendar id of the Family calendar (GET schedule-api /calendars). Empty disables the rule."
  type        = string
  default     = ""
}

variable "calendar_shared_id" {
  description = "Google Calendar id of the 'Ben | Cheryl' calendar — digest target for tasks tagged cheryl. Empty disables the rule."
  type        = string
  default     = ""
}
```

- [ ] **Step 2: Secret data source, IAM, env, scheduler, timeout**

In `terraform/secrets.tf`, add `"schedule-api-token", # schedule-api bearer auth (clients/schedule_api.py) — owned by schedule terraform` to the `shared` set.

Append to `terraform/iam.tf`:

```hcl
# /digest runs on the webhook CF only — it is the only caller of schedule-api.
resource "google_secret_manager_secret_iam_member" "webhook_cf_schedule_api_token" {
  secret_id = data.google_secret_manager_secret.shared["schedule-api-token"].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}
```

In `terraform/cloud_functions.tf` `common_env`, add:

```hcl
    SCHEDULE_API_URL          = var.schedule_api_url
    ASANA_PROJECT_FAMILY_GID  = var.asana_project_family_gid
    CALENDAR_FAMILY_ID        = var.calendar_family_id
    CALENDAR_SHARED_ID        = var.calendar_shared_id
```

In the `tasks_webhook` resource: change `timeout_seconds = 120` to `timeout_seconds = 300 # digest rebuild: listing + ≤40 Haiku calls + calendar writes`, and add after the `ASANA_ESCALATE_TOKEN` block:

```hcl
    secret_environment_variables {
      key        = "SCHEDULE_API_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["schedule-api-token"].secret_id
      version    = "latest"
    }
```

Update the comment above the resource to `# tasks-webhook — HTTP-triggered (public): Asana webhooks + /escalate and /digest crons`.

Append to `terraform/scheduler.tf`:

```hcl
# ---------------------------------------------------------------------------
# Due-day digest — every 10 minutes; the route itself decides whether to
# rebuild (dirty flag from the Asana webhook, or last rebuild > 60 min old).
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "digest" {
  name      = "tasks-digest"
  schedule  = "*/10 * * * *"
  time_zone = "America/Los_Angeles"

  http_target {
    http_method = "POST"
    uri         = "${google_cloudfunctions2_function.tasks_webhook.service_config[0].uri}/digest"
    body        = base64encode("{}")
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${var.tasks_escalate_token}"
    }
  }
}
```

Update `variable "tasks_escalate_token"`'s description to begin `"Bearer token Cloud Scheduler sends on POST /escalate and POST /digest — ..."`.

- [ ] **Step 3: tfvars example, CI, local env**

Append to `terraform/terraform.tfvars.example`:

```hcl
# Due-day digest routing (docs/superpowers/specs/2026-09-03-due-day-digest-design.md).
# Family Board project GID: numeric id in the project URL. Calendar ids: run
#   TOKEN=$(gcloud secrets versions access latest --secret=schedule-api-token --project bens-project-462804)
#   curl -s https://schedule-api.drolet.cloud/calendars -H "Authorization: Bearer $TOKEN"
# and copy calendar_id for "Family" and "Ben | Cheryl".
asana_project_family_gid = "..."
calendar_family_id       = "c_...@group.calendar.google.com"
calendar_shared_id       = "c_...@group.calendar.google.com"
```

In `.github/workflows/deploy.yml`, add under the existing `TF_VAR_inbox_api_url` line:

```yaml
          TF_VAR_asana_project_family_gid: ${{ vars.ASANA_PROJECT_FAMILY_GID }}
          TF_VAR_calendar_family_id: ${{ vars.CALENDAR_FAMILY_ID }}
          TF_VAR_calendar_shared_id: ${{ vars.CALENDAR_SHARED_ID }}
```

In `scripts/fetch-env.sh`, add before the `CLOUD_SQL_CONNECTION_NAME` line:

```bash
SCHEDULE_API_URL=https://schedule-api.drolet.cloud
SCHEDULE_API_TOKEN=$(secret schedule-api-token)
ASANA_PROJECT_FAMILY_GID=$(tfvar asana_project_family_gid)
CALENDAR_FAMILY_ID=$(tfvar calendar_family_id)
CALENDAR_SHARED_ID=$(tfvar calendar_shared_id)
```

- [ ] **Step 4: Validate terraform**

Run: `cd terraform && terraform init -backend=false -input=false >/dev/null && terraform validate && terraform fmt -check`
Expected: `Success! The configuration is valid.` and no fmt diff (run `terraform fmt` if it reports files).

- [ ] **Step 5: Smoke script**

Create `scripts/test-digest.py`:

```python
#!/usr/bin/env python3
"""Local run of the due-day digest against REAL Asana and REAL schedule-api.

Usage (from repo root, after scripts/fetch-env.sh):
    .venv/bin/python scripts/test-digest.py --dry-run      # print the plan, write nothing
    .venv/bin/python scripts/test-digest.py                # apply: creates/updates/deletes REAL events

--dry-run lists candidates, routes them, and prints desired events and the
diff against due_day_events without calling Claude or schedule-api. The real
run is exactly what POST /digest does with force=true; it shares the
digest_state row with production, so don't run it while the scheduler is
mid-rebuild (the job runs at :00, :10, :20 … — start between ticks).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.db import get_conn
from handlers import due_digest as h
from models.digest import DigestTask
from repo import due_digest as repo
from services import due_digest as dd
from services import task_bullets as tb


def dry_run() -> None:
    today = dd.today_local()
    routing = h._routing()
    tasks = [
        DigestTask(
            gid=t["gid"],
            name=t["name"],
            permalink_url=t.get("permalink_url") or "",
            due_on=t["due_on"],
            calendar_id=dd.route(t, **routing),
            points=tb.fallback_points(t.get("html_notes") or ""),
            links=tb.parse_links(t.get("html_notes") or ""),
        )
        for t in h._list_candidates()
        if dd.in_window(t, today)
    ]
    desired = dd.build_events(tasks)
    with get_conn() as conn:
        stored = repo.list_events(conn, since=today)
    plan = dd.plan(desired, stored, today)
    print(f"today={today} tasks_in_window={len(tasks)} desired_events={len(desired)}")
    for (day, cal), ev in sorted(desired.items()):
        print(f"  {day} {cal}: {ev.title}")
        for s in ev.sections:
            print(f"    - {s['title']}  ({len(s['points'])} pts, {len(s['links'])} links)")
    print(f"plan: create={len(plan.creates)} update={len(plan.updates)} delete={len(plan.deletes)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.dry_run:
        dry_run()
        return
    print(json.dumps(h.run(force=True), indent=2))


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Docs**

In `CLAUDE.md`, add a row to the Stack table after **Escalation**:

```
| **Digest** | Cloud Scheduler `tasks-digest`, `*/10 * * * *` → `POST <webhook-url>/digest` (same bearer as escalate) — rebuilds the due-day calendar digest when the Asana webhook has set `digest_state.dirty_at` or the last rebuild is > 60 min old; writes through `clients/schedule_api.py` (`SCHEDULE_API_URL`/`SCHEDULE_API_TOKEN`, secret owned by schedule terraform) |
```

Add `due_day_events`, `task_bullets`, `digest_state` to the **Database** row's table list. Add a section after "Recurring tasks":

```markdown
## Due-day digest

One all-day event per day that has open tasks due, for a rolling 30-day
window, on the calendar the task belongs to: Family Board (`ASANA_PROJECT_FAMILY_GID`)
→ Family (`CALENDAR_FAMILY_ID`); a `cheryl` tag → "Ben | Cheryl"
(`CALENDAR_SHARED_ID`); everything else → primary. Each task is a linked
title plus 2–3 Haiku-condensed bullets (cached in `task_bullets` by content
hash, ≤40 calls per rebuild) and the doc links from its Links section.
`services/due_digest.py` is the pure policy, `handlers/due_digest.py` the
rebuild, `repo/due_digest.py` the state. A completed task drops off its day;
a day with nothing due has no event. Routing ids are personal — they live
in `terraform.tfvars` and GitHub repo variables, never here. The Asana
webhook only flips a dirty flag (Asana wants a reply in 10 s); other
projects' edits land on the hourly rebuild. Design:
`docs/superpowers/specs/2026-09-03-due-day-digest-design.md`.
```

Add `.venv/bin/python scripts/test-digest.py --dry-run   # due-day digest plan; without the flag writes REAL events` to the Local dev block. In the Secrets section, change "the escalate token is the bearer credential Cloud Scheduler sends on `POST /escalate`" to "... on `POST /escalate` and `POST /digest`".

In `docs/asana-webhook-setup.md`, after the filters example, add:

```markdown
Optional: adding `"tags"` to the `changed` filter's `fields` makes a `cheryl`
tag change re-route the due-day digest within minutes; without it the change
lands on the hourly rebuild.
```

- [ ] **Step 7: Run everything and commit**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check .` (use whatever linters `pyproject.toml` configures; fix any findings).
Expected: all green.

```bash
git add terraform .github/workflows/deploy.yml scripts/fetch-env.sh scripts/test-digest.py CLAUDE.md docs/asana-webhook-setup.md && git commit -q -m "feat(digest): scheduler job, schedule-api secret wiring, routing vars, docs, smoke script"
```

---

### Task 13: rollout and live verification

**Files:** none new — this task runs the spec's Rollout section in order and records the result on the PR.

- [ ] **Step 1: Confirm the schedule PR is merged and deployed**

```bash
cd ~/src/schedule && gh pr view --json state,mergedAt
TOKEN=$(gcloud secrets versions access latest --secret=schedule-api-token --project bens-project-462804)
curl -s -X POST https://schedule-api.drolet.cloud/events -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"title":"x","date":"2030-01-01","sections":[{"title":"probe"}],"calendar":"primary","bogus":1}' | head -c 300
```
Expected: the `bogus` field yields a 422 that names only `bogus` (proving `sections` is accepted). No event is created because validation fails first.

- [ ] **Step 2: Migrate the tasks DB**

```bash
cd ~/src/tasks && scripts/fetch-env.sh && .venv/bin/python scripts/migrate_db.py
```
Expected: `Migration complete` (three additive `CREATE TABLE IF NOT EXISTS`).

- [ ] **Step 3: Fill tfvars and GitHub variables**

Add `asana_project_family_gid`, `calendar_family_id`, `calendar_shared_id` to `terraform/terraform.tfvars` (values per the tfvars.example comment). Then:

```bash
gh variable set ASANA_PROJECT_FAMILY_GID --body "$(grep asana_project_family_gid terraform/terraform.tfvars | sed 's/.*= *"\(.*\)"/\1/')"
gh variable set CALENDAR_FAMILY_ID --body "$(grep calendar_family_id terraform/terraform.tfvars | sed 's/.*= *"\(.*\)"/\1/')"
gh variable set CALENDAR_SHARED_ID --body "$(grep calendar_shared_id terraform/terraform.tfvars | sed 's/.*= *"\(.*\)"/\1/')"
scripts/fetch-env.sh
```

- [ ] **Step 4: Dry run, then a real local run**

```bash
.venv/bin/python scripts/test-digest.py --dry-run
.venv/bin/python scripts/test-digest.py
```
Expected: dry run prints desired events per (day, calendar) with plausible routing; the real run prints `"outcome": "ok"` with `created` equal to the desired count. Open the three calendars and confirm one all-day `N tasks due` event per listed day, titles clickable to Asana, transparent (shows as "free").

- [ ] **Step 5: Open the tasks PR and apply terraform**

Use the `/pr-open` skill. Then, before merging, use `/terraform-plan` and `/terraform-apply` from this repo so the scheduler job, secret binding, env vars and timeout exist before the code deploys (merge to `main` would apply them anyway via `deploy.yml`, but applying first lets you check the plan).

- [ ] **Step 6: Verify the live loop**

After merge and deploy: complete one task due within the window in Asana. Within ~10 minutes its day's event should update (or disappear if it was the only task). Check:

```bash
gcloud functions logs read tasks-webhook --region us-central1 --project bens-project-462804 --limit 50 | grep -i digest
```
Expected: a `Digest rebuild ok:` line with `updated` or `deleted` ≥ 1. Use the `querying-grafana-metrics` skill to confirm `asana_digest_rebuilds_total` is landing.

- [ ] **Step 7: Post the verification on the PR**

Comment on the PR with the dry-run summary, the real-run JSON, and what the calendars show. Use the `verifying-pr-locally` skill's reporting format.

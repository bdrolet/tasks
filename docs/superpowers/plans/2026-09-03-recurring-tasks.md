# Recurring Tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A task tagged `repeat:3mo` in Asana creates its next occurrence — due three months after the completion date — when it is completed.

**Architecture:** The repeat rule lives in an Asana tag, not a database table, so it is visible in the UI, settable through the tags fields the API already has, and durable against a Postgres outage. `services/recurrence.py` owns the grammar and the successor creation; `handlers/task_complete.py` gains one guarded call before it moves the completed task to Done. Idempotency is enforced Asana-side via `external.gid = "recur:<completed gid>"`.

**Tech Stack:** Python 3.13, `dateutil.relativedelta` (new runtime dependency), Asana REST via `clients/asana.py`, pytest with `monkeypatch`, OpenTelemetry counters.

**Spec:** `docs/superpowers/specs/2026-09-03-recurring-tasks-design.md`

## Global Constraints

- **Python 3.13**, ruff (`select = ["E", "F", "I"]`, line-length 100, `E501` ignored), mypy with `python_version = "3.13"`.
- **Layer rules** (CLAUDE.md): `clients/` is I/O only; `services/` is business logic, one concern per file, no direct HTTP; `handlers/` orchestrate and are called only from `main.py`; `models/` are pure types.
- **Nothing in this feature may crash a completion event.** Every recurrence call site is wrapped; a failure logs and the task still gets marked completed and moved to Done.
- **The only new runtime dependency is `python-dateutil`.** It goes in both `requirements.txt` and `requirements-dev.txt` — CI installs only the dev file, so a runtime-only addition type-checks against a different dependency set than production runs.
- **No DB migration, no Terraform change.** If you find yourself writing SQL, re-read the spec's D1.
- **Run tests with** `.venv/bin/pytest tests/ -q`. Lint with `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .`; types with `.venv/bin/mypy .`.
- **Branch:** work on `feat/recurring-tasks`, already created off `main`. Open a PR with the `/pr-open` skill; never commit to `main`.
- **Never commit a declared fact** (personal context) to this repo — it is public.

---

## File Structure

| File | Responsibility |
|---|---|
| `services/recurrence.py` | **new.** The `repeat:` grammar, rule lookup, due-date arithmetic, and successor creation. One concern: recurrence. |
| `handlers/task_complete.py` | Gains a guarded recurrence step between the completed-check and the Done move. |
| `clients/asana.py` | `get_task` returns tags and `completed_at` so the tag check costs no extra API call. |
| `clients/otel.py` | `asana.recurrences` counter. |
| `api/routers/tasks.py` | Rejects malformed `repeat:*` tags with a 400 instead of creating a dead tag. |
| `tests/test_recurrence.py` | **new.** Grammar, due-date arithmetic, successor field-building, idempotency. |
| `tests/test_task_complete.py` | Handler wiring and failure isolation. |
| `tests/test_api_tasks.py` | Tag validation. |
| `requirements.txt`, `requirements-dev.txt` | `python-dateutil`. |
| `CLAUDE.md`, `.claude/skills/{creating,editing}-tasks/SKILL.md`, `.claude/agents/task-builder.md` | Document the tag for humans and agents. |

---

### Task 1: The `repeat:` grammar

**Files:**
- Create: `services/recurrence.py`
- Create: `tests/test_recurrence.py`
- Modify: `requirements.txt`, `requirements-dev.txt`

**Interfaces:**
- Consumes: nothing.
- Produces: `services.recurrence.TAG_PREFIX: str` (`"repeat:"`), `parse(tag_name: str) -> relativedelta | None`, `find_rule(tags: list[dict]) -> tuple[str, relativedelta] | None` returning `(tag_gid, interval)`.

**Context you need:** Asana tag dicts in this repo have the shape `{"gid": "...", "name": "..."}` — see `DETAIL_OPT_FIELDS` in `clients/asana.py`, which requests `tags.gid,tags.name`.

- [ ] **Step 1: Declare the dependency**

Add to `requirements.txt`, after the `google-auth` block:

```
# Calendar-aware interval arithmetic for recurring tasks (relativedelta handles
# months/years, which timedelta deliberately does not)
python-dateutil>=2.9
```

Add the same line (without the comment) to `requirements-dev.txt` in the runtime-deps section at the bottom, beside `psycopg[binary]>=3.1`:

```
python-dateutil>=2.9
```

Then install it: `.venv/bin/pip install -r requirements-dev.txt`

- [ ] **Step 2: Write the failing tests**

Create `tests/test_recurrence.py`:

```python
import pytest
from dateutil.relativedelta import relativedelta

from services import recurrence


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("repeat:10d", relativedelta(days=10)),
        ("repeat:1day", relativedelta(days=1)),
        ("repeat:3days", relativedelta(days=3)),
        ("repeat:2w", relativedelta(weeks=2)),
        ("repeat:2weeks", relativedelta(weeks=2)),
        ("repeat:3mo", relativedelta(months=3)),
        ("repeat:3mon", relativedelta(months=3)),
        ("repeat:1month", relativedelta(months=1)),
        ("repeat:6months", relativedelta(months=6)),
        ("repeat:1y", relativedelta(years=1)),
        ("repeat:2yr", relativedelta(years=2)),
        ("repeat:1year", relativedelta(years=1)),
        ("REPEAT:3MO", relativedelta(months=3)),
        ("repeat: 3 mo ", relativedelta(months=3)),
    ],
)
def test_parse_accepts_every_unit_and_alias(tag, expected):
    assert recurrence.parse(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "repeat:3m",  # bare m — ambiguous between minutes and months
        "repeat:0d",
        "repeat:-3mo",
        "repeat:3651d",
        "repeat:mo",
        "repeat:3",
        "repeat:three months",
        "repeat:",
        "repeat:3fortnights",
        "urgent",  # not a repeat tag at all
        "",
    ],
)
def test_parse_rejects_malformed_rules(tag):
    assert recurrence.parse(tag) is None


def test_find_rule_returns_gid_and_interval():
    tags = [{"gid": "t1", "name": "home"}, {"gid": "t2", "name": "repeat:3mo"}]
    assert recurrence.find_rule(tags) == ("t2", relativedelta(months=3))


def test_find_rule_without_a_repeat_tag_is_none():
    assert recurrence.find_rule([{"gid": "t1", "name": "home"}]) is None
    assert recurrence.find_rule([]) is None


def test_two_repeat_tags_are_ambiguous_and_ignored(caplog):
    tags = [{"gid": "t1", "name": "repeat:3mo"}, {"gid": "t2", "name": "repeat:1y"}]
    assert recurrence.find_rule(tags) is None
    assert "repeat tags" in caplog.text


def test_unparseable_repeat_tag_is_ignored():
    assert recurrence.find_rule([{"gid": "t1", "name": "repeat:3months?"}]) is None
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recurrence.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'services.recurrence'`

- [ ] **Step 4: Write the implementation**

Create `services/recurrence.py`:

```python
"""Completion-anchored task recurrence.

The rule lives in an Asana tag: `repeat:3mo` means "when this task is
completed, create the next occurrence due three months after the completion
date". Asana is the source of truth — there is no DB row, so a Postgres
outage cannot silently end a chain.

Design: docs/superpowers/specs/2026-09-03-recurring-tasks-design.md
"""

import logging
import re

from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

TAG_PREFIX = "repeat:"

# Bare "m" is deliberately absent: ambiguous between minutes and months, and
# guessing wrong schedules the next occurrence 30x early or late.
_UNITS = {
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
    "mo": "months",
    "mon": "months",
    "month": "months",
    "months": "months",
    "y": "years",
    "yr": "years",
    "year": "years",
    "years": "years",
}
_MAX_COUNT = 3650  # ~10 years; a larger number is a typo, not an intention
_RULE = re.compile(r"^\s*(\d+)\s*([a-z]+)\s*$")


def parse(tag_name: str) -> relativedelta | None:
    """`repeat:3mo` -> relativedelta(months=3).

    Returns None for anything that is not a well-formed rule, including tags
    that are not repeat tags at all. Never raises: a typo'd tag must not take
    down a completion event."""
    name = (tag_name or "").strip().casefold()
    if not name.startswith(TAG_PREFIX):
        return None
    match = _RULE.match(name[len(TAG_PREFIX) :])
    unit = _UNITS.get(match.group(2)) if match else None
    if match is None or unit is None:
        logger.warning("Unparseable repeat tag %r — ignoring", tag_name)
        return None
    count = int(match.group(1))
    if not 1 <= count <= _MAX_COUNT:
        logger.warning("Repeat tag %r has an out-of-range count — ignoring", tag_name)
        return None
    return relativedelta(**{unit: count})


def find_rule(tags: list[dict]) -> tuple[str, relativedelta] | None:
    """(tag_gid, interval) for a task's repeat tag, or None.

    None when there is no repeat tag, when the one present does not parse, or
    when there is more than one — guessing which of two rules was meant is
    worse than doing nothing and letting the tags be corrected."""
    candidates = [
        t
        for t in tags or []
        if (t.get("name") or "").strip().casefold().startswith(TAG_PREFIX)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "Task carries %d repeat tags (%s) — ignoring all",
            len(candidates),
            [t.get("name") for t in candidates],
        )
        return None
    interval = parse(candidates[0].get("name") or "")
    if interval is None:
        return None
    return candidates[0]["gid"], interval
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_recurrence.py -q`
Expected: PASS (all parametrized cases)

- [ ] **Step 6: Lint and type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy services/recurrence.py`
Expected: clean. If `ruff format --check` complains, run `.venv/bin/ruff format services/recurrence.py tests/test_recurrence.py` and re-run.

- [ ] **Step 7: Commit**

```bash
git add services/recurrence.py tests/test_recurrence.py requirements.txt requirements-dev.txt
git commit -m "feat: parse repeat: tags into relativedelta intervals"
```

---

### Task 2: Completion timestamp to due date

**Files:**
- Modify: `services/recurrence.py`
- Test: `tests/test_recurrence.py`

**Interfaces:**
- Consumes: `parse` / `find_rule` from Task 1.
- Produces: `next_due(completed_at: str | None, interval: relativedelta) -> datetime.date`, `LOCAL_TZ: ZoneInfo`.

**Context you need:** Asana returns `completed_at` as a UTC ISO-8601 string, e.g. `"2026-09-03T23:30:00.000Z"`. This repo has no timezone handling anywhere today — `zoneinfo` here is new, and stays local to this module. `America/New_York` is the same zone already configured for the `tasks-escalation` Cloud Scheduler job.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recurrence.py`:

```python
from datetime import date


def test_next_due_adds_days():
    assert recurrence.next_due(
        "2026-09-03T14:00:00.000Z", relativedelta(days=10)
    ) == date(2026, 9, 13)


def test_next_due_clamps_to_end_of_month():
    # Jan 31 + 1 month has no Feb 31 to land on; relativedelta clamps.
    assert recurrence.next_due(
        "2026-01-31T14:00:00.000Z", relativedelta(months=1)
    ) == date(2026, 2, 28)


def test_next_due_handles_leap_year():
    assert recurrence.next_due(
        "2024-01-31T14:00:00.000Z", relativedelta(months=1)
    ) == date(2024, 2, 29)


def test_next_due_uses_the_local_completion_date_not_utc():
    # 23:30 UTC on the 3rd is 19:30 ET on the 3rd. Taking the UTC date would
    # date the successor a day late.
    assert recurrence.next_due(
        "2026-09-03T23:30:00.000Z", relativedelta(days=1)
    ) == date(2026, 9, 4)


def test_next_due_treats_a_naive_timestamp_as_utc():
    assert recurrence.next_due(
        "2026-09-03T23:30:00", relativedelta(days=1)
    ) == date(2026, 9, 4)


def test_next_due_falls_back_to_today_without_a_timestamp():
    assert recurrence.next_due(None, relativedelta(days=1)) == date.today() + relativedelta(days=1)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recurrence.py -q -k next_due`
Expected: FAIL with `AttributeError: module 'services.recurrence' has no attribute 'next_due'`

- [ ] **Step 3: Write the implementation**

In `services/recurrence.py`, extend the imports:

```python
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
```

Add after the `_RULE` constant:

```python
# Asana timestamps are UTC. An 8pm ET completion is already tomorrow in UTC,
# which would date the successor a day late — so the date is taken locally.
# This is the repo's only timezone-aware code; it stays scoped to recurrence.
LOCAL_TZ = ZoneInfo("America/New_York")
```

Add after `find_rule`:

```python
def next_due(completed_at: str | None, interval: relativedelta) -> date:
    """Local completion date + interval.

    Falls back to today when Asana gave us no completed_at — a successor due
    on a slightly wrong day beats no successor at all."""
    if not completed_at:
        return date.today() + interval
    moment = datetime.fromisoformat(completed_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date() + interval
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_recurrence.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/recurrence.py tests/test_recurrence.py
git commit -m "feat: compute the next occurrence date from the local completion date"
```

---

### Task 3: Create the successor

**Files:**
- Modify: `services/recurrence.py`
- Modify: `clients/otel.py:15-38` (instrument declarations), `clients/otel.py:45-49` (globals), `clients/otel.py:84` (instrument creation)
- Test: `tests/test_recurrence.py`

**Interfaces:**
- Consumes: `find_rule`, `next_due`, `LOCAL_TZ` from Tasks 1–2. From `clients/asana.py`: `find_task_by_external(external_gid) -> str | None`, `create_task_from_fields(fields: dict) -> CreatedTask` (a `CreatedTask` has `.gid` and `.permalink_url`), `add_task_to_section(task_gid, section_gid) -> None`, `remove_tag(task_gid, tag_gid) -> None`, `create_story(task_gid, *, text=None, html_text=None) -> dict`, and the module-level `ASANA_PROJECT_ID`. From `services/sections.py`: `done() -> str | None`. From `services/task_index.py`: `refresh(task_gid) -> None` (already internally best-effort — call it unguarded, as `api/routers/tasks.py` does).
- Produces: `EXTERNAL_PREFIX: str` (`"recur:"`), `spawn_next(task: dict, detail: dict, section: dict | None, rule: tuple[str, relativedelta]) -> str | None` returning the new gid, or `None` when a successor already exists.

**Context you need:**
- `task` is the light dict from `asana.get_task` (after Task 4 it carries `gid`, `name`, `completed`, `completed_at`, `tags`, `memberships`). `detail` is the rich dict from `asana.get_task_detail` (`html_notes`, `tags`, `assignee.gid`, `due_on`, …).
- `section` is `{"gid": ..., "name": ...}` or `None` — captured by the handler *before* the Done move.
- Asana's create payload takes `external: {"gid": "..."}`. Looking a task up later uses the path `/tasks/external:<gid>`, which `find_task_by_external` already wraps.
- `services/escalation.py` is the precedent for a service that calls `clients/asana.py` directly. That is allowed; the layer rule forbids raw HTTP, not client calls.

- [ ] **Step 1: Add the metric instrument**

In `clients/otel.py`, add to the no-op declarations beside `tasks_related` (around line 37):

```python
recurrences: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

Extend the `global` statement at line 49:

```python
    global tasks_screened, tasks_related, recurrences
```

And create the real instrument after `tasks_related` in `setup_telemetry`:

```python
    recurrences = meter.create_counter(
        "asana.recurrences", description="Successor tasks created from a repeat: tag"
    )
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_recurrence.py`. Add `import clients.asana as asana` to the imports at the top of the file — and nothing else, or ruff's `F401` will flag the unused name:

```python
class FakeCreated:
    def __init__(self, gid: str, permalink_url: str):
        self.gid = gid
        self.permalink_url = permalink_url


@pytest.fixture
def asana_stub(monkeypatch):
    """Records every Asana write the successor path makes."""
    calls: dict = {"created": [], "sections": [], "removed_tags": [], "stories": [], "refreshed": []}
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj-1")
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: None)
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: (
            calls["created"].append(fields),
            FakeCreated("new-1", "https://app.asana.com/0/1/new-1"),
        )[1],
    )
    monkeypatch.setattr(
        asana, "add_task_to_section", lambda t, s: calls["sections"].append((t, s))
    )
    monkeypatch.setattr(asana, "remove_tag", lambda t, g: calls["removed_tags"].append((t, g)))
    monkeypatch.setattr(
        asana, "create_story", lambda gid, **kw: calls["stories"].append((gid, kw)) or {}
    )
    monkeypatch.setattr(
        recurrence.task_index, "refresh", lambda gid: calls["refreshed"].append(gid)
    )
    return calls


def _task(**over):
    base = {
        "gid": "old-1",
        "name": "[P2] Change the furnace filter",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
    }
    return {**base, **over}


def _detail(**over):
    base = {
        "name": "[P2] Change the furnace filter",
        "html_notes": "<body>Filter size 16x25x1</body>",
        "tags": [{"gid": "t-home", "name": "home"}, {"gid": "t2", "name": "repeat:3mo"}],
        "assignee": {"gid": "u-1", "name": "Ben"},
    }
    return {**base, **over}


def test_spawn_next_creates_a_dated_copy(asana_stub, monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    new_gid = recurrence.spawn_next(
        _task(), _detail(), {"gid": "sec-review", "name": "Review"}, ("t2", relativedelta(months=3))
    )

    assert new_gid == "new-1"
    fields = asana_stub["created"][0]
    assert fields["name"] == "[P2] Change the furnace filter"
    assert fields["html_notes"] == "<body>Filter size 16x25x1</body>"
    assert fields["due_on"] == "2026-12-03"
    assert fields["external"] == {"gid": "recur:old-1"}
    assert fields["projects"] == ["proj-1"]
    assert fields["assignee"] == "u-1"
    # The repeat tag rides along, so the successor inherits the rule.
    assert set(fields["tags"]) == {"t-home", "t2"}


def test_spawn_next_places_the_successor_in_the_previous_section(asana_stub, monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    recurrence.spawn_next(
        _task(), _detail(), {"gid": "sec-review", "name": "Review"}, ("t2", relativedelta(months=3))
    )
    assert asana_stub["sections"] == [("new-1", "sec-review")]


def test_spawn_next_leaves_the_successor_unsectioned_when_the_old_one_was_in_done(
    asana_stub, monkeypatch
):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    recurrence.spawn_next(
        _task(), _detail(), {"gid": "sec-done", "name": "Done"}, ("t2", relativedelta(months=3))
    )
    assert asana_stub["sections"] == []


def test_spawn_next_strips_the_tag_and_links_forward(asana_stub, monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    recurrence.spawn_next(_task(), _detail(), None, ("t2", relativedelta(months=3)))

    assert asana_stub["removed_tags"] == [("old-1", "t2")]
    gid, kwargs = asana_stub["stories"][0]
    assert gid == "old-1"
    assert "https://app.asana.com/0/1/new-1" in kwargs["text"]
    assert asana_stub["refreshed"] == ["new-1"]


def test_spawn_next_is_idempotent_via_the_external_gid(asana_stub, monkeypatch):
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: "already-there")
    assert recurrence.spawn_next(_task(), _detail(), None, ("t2", relativedelta(months=3))) is None
    assert asana_stub["created"] == []
    assert asana_stub["removed_tags"] == []


def test_a_failed_tag_strip_does_not_lose_the_successor(asana_stub, monkeypatch):
    def boom(task_gid, tag_gid):
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "remove_tag", boom)
    assert recurrence.spawn_next(_task(), _detail(), None, ("t2", relativedelta(months=3))) == "new-1"


def test_spawn_next_copies_no_comments_or_subtasks(asana_stub):
    recurrence.spawn_next(
        _task(), _detail(num_subtasks=3), None, ("t2", relativedelta(months=3))
    )
    fields = asana_stub["created"][0]
    assert "parent" not in fields
    assert "num_subtasks" not in fields
    assert "due_at" not in fields
    assert "completed" not in fields
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recurrence.py -q -k spawn`
Expected: FAIL with `AttributeError: module 'services.recurrence' has no attribute 'spawn_next'`

- [ ] **Step 4: Write the implementation**

Extend the imports at the top of `services/recurrence.py`:

```python
import clients.asana as asana
import clients.otel as otel
from services import sections, task_index
```

Add after the `LOCAL_TZ` constant:

```python
# Successors carry external.gid = "recur:<completed gid>". This is the
# idempotency guard, and it is deliberately in Asana rather than Postgres:
# a webhook redelivery or an uncomplete/recomplete must not create a second
# occurrence even while the database is unreachable.
EXTERNAL_PREFIX = "recur:"
```

Add at the end of the module:

```python
def spawn_next(
    task: dict,
    detail: dict,
    section: dict | None,
    rule: tuple[str, relativedelta],
) -> str | None:
    """Create the successor to a just-completed recurring task.

    Returns the new gid, or None when a successor already exists. `section`
    is where the completed task lived *before* it was moved to Done — the
    successor goes back there."""
    tag_gid, interval = rule
    gid = task["gid"]
    external = f"{EXTERNAL_PREFIX}{gid}"

    existing = asana.find_task_by_external(external)
    if existing:
        logger.info("Recurrence for %s already created as %s — skipping", gid, existing)
        return None

    fields: dict = {
        "name": detail.get("name") or task.get("name") or "",
        "html_notes": detail.get("html_notes") or "<body></body>",
        "due_on": next_due(task.get("completed_at"), interval).isoformat(),
        "external": {"gid": external},
    }
    tag_gids = [t["gid"] for t in detail.get("tags") or [] if t.get("gid")]
    if tag_gids:
        fields["tags"] = tag_gids
    assignee_gid = (detail.get("assignee") or {}).get("gid")
    if assignee_gid:
        fields["assignee"] = assignee_gid
    if asana.ASANA_PROJECT_ID:
        fields["projects"] = [asana.ASANA_PROJECT_ID]

    created = asana.create_task_from_fields(fields)
    otel.recurrences.add(1)
    logger.info("Recurring task %s → %s due %s", gid, created.gid, fields["due_on"])

    # The successor belongs where the last occurrence lived. If that was Done
    # (dragged there by hand), leave it unsectioned rather than filing a chore
    # under a mail-routing default.
    if section and section["gid"] != sections.done():
        _try("place the successor in a section", asana.add_task_to_section, created.gid, section["gid"])

    # Create-then-strip: the successor already exists, so a failed strip only
    # risks a duplicate on re-complete, which the external-gid guard catches.
    # The reverse order would risk a dead chain with nothing recorded.
    _try("strip the repeat tag", asana.remove_tag, gid, tag_gid)
    _try(
        "post the forward link",
        asana.create_story,
        gid,
        text=f"↻ Next occurrence: {created.permalink_url}",
    )
    task_index.refresh(created.gid)
    return created.gid


def _try(what: str, fn, *args, **kwargs) -> None:
    """Run a follow-up Asana call that must not cost us the successor."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Recurrence: failed to %s", what)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_recurrence.py -q`
Expected: PASS

- [ ] **Step 6: Verify the whole suite still passes**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS. `tests/test_otel.py` touches the instrument list — if it asserts on a count of instruments, update it to include `recurrences`.

- [ ] **Step 7: Lint and type-check**

Run: `.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add services/recurrence.py clients/otel.py tests/test_recurrence.py tests/test_otel.py
git commit -m "feat: create the next occurrence when a repeat: task completes"
```

---

### Task 4: Wire it into the completion handler

**Files:**
- Modify: `clients/asana.py:186-197` (`get_task`)
- Modify: `handlers/task_complete.py`
- Test: `tests/test_task_complete.py`, `tests/test_asana_client.py`

**Interfaces:**
- Consumes: `recurrence.find_rule`, `recurrence.spawn_next` from Tasks 1–3. From `clients/asana.py`: `get_task_detail(gid) -> dict | None`, `current_section(task) -> dict | None`.
- Produces: no new public interface — this is the wiring.

**Context you need:** `handlers/task_complete.py::handle` currently fetches the task, returns early on an un-complete event, writes the DB rows, then moves the task to Done. The recurrence step must run **before** the Done move, because `current_section` is how we learn where the successor belongs and the move overwrites it.

- [ ] **Step 1: Write the failing test for the widened `get_task` fields**

Add to `tests/test_asana_client.py`:

```python
def test_get_task_requests_tags_and_completed_at(monkeypatch):
    calls = _capture(monkeypatch, _resp(200, {"data": {"gid": "1"}}))
    asana.get_task("1")
    fields = calls[0]["params"]["opt_fields"]
    assert "tags.gid" in fields
    assert "tags.name" in fields
    assert "completed_at" in fields
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_asana_client.py -q -k get_task_requests`
Expected: FAIL — `assert 'tags.gid' in 'completed,name,memberships.section.gid,...'`

- [ ] **Step 3: Widen `get_task`**

In `clients/asana.py`, replace the `opt_fields` value in `get_task`:

```python
        params={
            "opt_fields": "completed,completed_at,name,tags.gid,tags.name,"
            "memberships.section.gid,memberships.section.name,memberships.project.gid"
        },
```

Tags come back on the call the handler already makes, so checking for a
repeat rule costs nothing on the overwhelmingly common non-recurring
completion.

- [ ] **Step 4: Run it to verify it passes**

Run: `.venv/bin/pytest tests/test_asana_client.py -q`
Expected: PASS

- [ ] **Step 5: Write the failing handler tests**

Add to `tests/test_task_complete.py`:

```python
from dateutil.relativedelta import relativedelta

from services import recurrence


def _wire_completion(monkeypatch, task):
    """The minimum stubs for handle() to reach the Done move."""
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_task", lambda gid: task)
    monkeypatch.setattr(asana, "current_section", lambda t: {"gid": "s-review", "name": "Review"})
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))
    return moves


def test_repeat_tag_spawns_the_next_occurrence(monkeypatch):
    task = {
        "gid": "42",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
        "tags": [{"gid": "t2", "name": "repeat:3mo"}],
    }
    moves = _wire_completion(monkeypatch, task)
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: {"name": "x", "tags": []})
    spawned = []
    monkeypatch.setattr(
        recurrence,
        "spawn_next",
        lambda t, d, s, rule: spawned.append((t["gid"], s, rule)) or "new-1",
    )

    task_complete.handle("42")

    assert spawned == [("42", {"gid": "s-review", "name": "Review"}, ("t2", relativedelta(months=3)))]
    assert moves == [("42", "sec-done")]  # the Done move still happens


def test_completion_without_a_repeat_tag_touches_no_recurrence_code(monkeypatch):
    task = {"gid": "42", "completed": True, "tags": [{"gid": "t1", "name": "home"}]}
    moves = _wire_completion(monkeypatch, task)

    def fail(*a, **k):
        raise AssertionError("get_task_detail must not be called without a repeat tag")

    monkeypatch.setattr(asana, "get_task_detail", fail)

    task_complete.handle("42")
    assert moves == [("42", "sec-done")]


def test_a_failing_recurrence_still_completes_and_moves_the_task(monkeypatch):
    task = {
        "gid": "42",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
        "tags": [{"gid": "t2", "name": "repeat:3mo"}],
    }
    moves = _wire_completion(monkeypatch, task)
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: {"name": "x", "tags": []})

    def boom(*a, **k):
        raise RuntimeError("asana down")

    monkeypatch.setattr(recurrence, "spawn_next", boom)

    task_complete.handle("42")
    assert moves == [("42", "sec-done")]
```

- [ ] **Step 6: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_task_complete.py -q -k "repeat or recurrence"`
Expected: FAIL — `spawned == []`, because nothing calls `spawn_next` yet.

- [ ] **Step 7: Wire the handler**

In `handlers/task_complete.py`, add the import:

```python
from services import recurrence, sections
```

Insert this block immediately after `otel.tasks_completed.add(1)` and before the DB writes:

```python
    # Recurrence runs before the Done move: current_section is how the
    # successor learns where to live, and the move overwrites it. Guarded —
    # a recurrence failure must never cost us the completion itself.
    rule = recurrence.find_rule(task.get("tags") or [])
    if rule:
        try:
            detail = asana.get_task_detail(task_gid) or {}
            recurrence.spawn_next(task, detail, asana.current_section(task), rule)
        except Exception:
            logger.exception("Recurrence failed for gid=%s — completion continues", task_gid)
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_task_complete.py -q`
Expected: PASS

- [ ] **Step 9: Run the whole suite, lint, and type-check**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add clients/asana.py handlers/task_complete.py tests/test_task_complete.py tests/test_asana_client.py
git commit -m "feat: spawn the next occurrence from the completion webhook"
```

---

### Task 5: Reject malformed `repeat:` tags at the API

**Files:**
- Modify: `api/routers/tasks.py`
- Test: `tests/test_api_tasks.py`

**Interfaces:**
- Consumes: `recurrence.parse`, `recurrence.TAG_PREFIX` from Task 1.
- Produces: `_validate_repeat_tags(tag_names: list[str]) -> None` (module-private; raises `HTTPException(400)`).

**Context you need:** `POST /tasks` takes `tags: list[str]`; `PATCH /tasks/{gid}` takes `add_tags: list[str]`. Both resolve names to GIDs through `services/tags.py::resolve_gids`, which **creates the tag if it does not exist**. That is what makes validation worth having: without it, `repeat:3months` (or `repeat:3m`) silently creates a real workspace tag that looks live in the UI and does nothing forever. This is the single most likely way the feature quietly fails.

Validate before any Asana I/O, the way `_title` already validates priority.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api_tasks.py`. That file uses a **module-level** `client = TestClient(app)` and an `AUTH` header dict — not a fixture — and `_patch_env` is its existing helper for PATCH tests. The 400 cases need no Asana stubs at all: validation runs before any I/O.

```python
def test_create_rejects_a_malformed_repeat_tag():
    resp = client.post(
        "/tasks", json={"name": "Change the filter", "tags": ["repeat:3months?"]}, headers=AUTH
    )
    assert resp.status_code == 400
    assert "repeat:" in str(resp.json()["detail"])


def test_create_rejects_bare_m():
    resp = client.post(
        "/tasks", json={"name": "Change the filter", "tags": ["repeat:3m"]}, headers=AUTH
    )
    assert resp.status_code == 400


def test_patch_rejects_a_malformed_repeat_tag(monkeypatch):
    _patch_env(monkeypatch)
    resp = client.patch("/tasks/t1", json={"add_tags": ["repeat:soon"]}, headers=AUTH)
    assert resp.status_code == 400


def test_a_valid_repeat_tag_is_accepted(monkeypatch):
    from api.routers import tasks as tasks_router

    captured = {}
    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: captured.update(fields)
        or CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    monkeypatch.setattr(tasks_router.tags_service, "resolve_gids", lambda names: ["tg-repeat"])

    resp = client.post(
        "/tasks", json={"name": "Change the filter", "tags": ["repeat:3mo"]}, headers=AUTH
    )
    assert resp.status_code == 201
    assert captured["tags"] == ["tg-repeat"]


def test_ordinary_tags_are_untouched(monkeypatch):
    from api.routers import tasks as tasks_router

    monkeypatch.setattr(asana, "list_projects", lambda: [{"gid": "p-email", "name": "Inbox"}])
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: CreatedTask(gid="t9", permalink_url="https://a/t9"),
    )
    monkeypatch.setattr(tasks_router.tags_service, "resolve_gids", lambda names: ["tg1", "tg2"])

    resp = client.post(
        "/tasks", json={"name": "Change the filter", "tags": ["home", "urgent"]}, headers=AUTH
    )
    assert resp.status_code == 201
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/pytest tests/test_api_tasks.py -q -k repeat`
Expected: FAIL — the malformed cases return 201 instead of 400.

- [ ] **Step 3: Implement the validation**

In `api/routers/tasks.py`, add the import:

```python
from services import recurrence
```

Add beside `_title`:

```python
def _validate_repeat_tags(tag_names: list[str]) -> None:
    """400 on a malformed repeat: tag rather than creating a dead one.

    tags_service.resolve_gids creates tags that do not exist, so an
    unvalidated `repeat:3months` becomes a real workspace tag that looks live
    in the UI and never fires."""
    for name in tag_names:
        if name.strip().casefold().startswith(recurrence.TAG_PREFIX) and recurrence.parse(name) is None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": f"malformed repeat tag: {name}",
                    "expected": "repeat:<count><unit>, unit one of d|w|mo|y "
                    "(e.g. repeat:10d, repeat:2w, repeat:3mo, repeat:1y)",
                },
            )
```

Call it in `create_task`, immediately after the `_title` line:

```python
    _validate_repeat_tags(body.tags)
```

And in `patch_task`, immediately after the priority/name check at the top:

```python
    _validate_repeat_tags(body.add_tags)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_tasks.py -q`
Expected: PASS

- [ ] **Step 5: Run the whole suite, lint, and type-check**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add api/routers/tasks.py tests/test_api_tasks.py
git commit -m "feat: reject malformed repeat: tags with a 400 instead of creating them"
```

---

### Task 6: Document the tag for humans and agents

**Files:**
- Modify: `CLAUDE.md`
- Modify: `.claude/skills/creating-tasks/SKILL.md`, `.claude/skills/editing-tasks/SKILL.md`
- Modify: `.claude/agents/task-builder.md`

**Interfaces:**
- Consumes: the finished behavior from Tasks 1–5.
- Produces: nothing executable.

**Context you need:** Read each file before editing — match its existing heading level, tone, and length. These skills are symlinked into `~/.claude/` by `scripts/link-skills.sh`, so an edit here is live for the agents immediately; no deploy.

- [ ] **Step 1: Add a section to `CLAUDE.md`**

Insert after the "Section mapping" section:

```markdown
## Recurring tasks

A task tagged `repeat:3mo` creates its next occurrence when it is completed,
due `completion date + interval` — completion-anchored, not calendar-anchored.
The rule lives in the Asana tag, not the database: `services/recurrence.py`
parses it, `handlers/task_complete.py` acts on it before the Done move.
Grammar is `repeat:<count><unit>` with unit `d`/`w`/`mo`/`y` (spelled-out
aliases accepted; bare `m` rejected as ambiguous). Set or clear it with the
ordinary `add_tags`/`remove_tags` fields, or by hand in Asana.

The successor copies name, description, project, section, tags and assignee —
not comments, subtasks, attachments or time-of-day. It carries
`external.gid = recur:{completed_gid}`, which is the idempotency guard against
webhook redelivery and uncomplete/recomplete. Completing strips the `repeat:`
tag from the finished occurrence, so exactly one open task per series carries
it. Design:
`docs/superpowers/specs/2026-09-03-recurring-tasks-design.md`.
```

- [ ] **Step 2: Add the tag to the two consumer skills**

In `.claude/skills/creating-tasks/SKILL.md` and `.claude/skills/editing-tasks/SKILL.md`, add to whichever section documents tags:

```markdown
**Recurring tasks.** A `repeat:<count><unit>` tag (`repeat:10d`, `repeat:2w`,
`repeat:3mo`, `repeat:1y`) makes the task come back: completing it creates the
next occurrence, due that interval after the completion date. Pass it in
`tags` / `add_tags` like any other tag; remove it to stop the series. A
malformed rule is rejected with a 400 — bare `m` is not accepted, use `mo`.
```

- [ ] **Step 3: Add it to the task-builder agent**

In `.claude/agents/task-builder.md`, add one line where the agent is told what fields to fill:

```markdown
- If the request describes something that comes back on a cadence after it is
  done ("every three months", "again a week after I finish"), add a
  `repeat:<count><unit>` tag — `repeat:3mo`, `repeat:1w`. Recurrence is
  completion-anchored: do not use it for a fixed calendar schedule.
```

- [ ] **Step 4: Verify nothing else claims tasks are one-shot**

Run: `grep -rn "recur\|repeat" CLAUDE.md README.md docs/task-content-standard.md`
Expected: only the new text. If `docs/task-content-standard.md` describes the tag vocabulary, add a one-line pointer there too.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .claude/skills/creating-tasks/SKILL.md .claude/skills/editing-tasks/SKILL.md .claude/agents/task-builder.md
git commit -m "docs: document the repeat: tag for humans and consumer agents"
```

---

## Manual verification before merge

Automated tests never touch Asana. Before merging, use the
`verifying-pr-locally` skill and confirm the real round trip:

- [ ] `scripts/fetch-env.sh`, then run the API locally:
  `(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080)`
- [ ] Create a real task with `tags: ["repeat:2d"]` via `POST /tasks` and confirm the tag appears on it in Asana.
- [ ] Confirm `POST /tasks` with `tags: ["repeat:2days!"]` returns 400.
- [ ] Complete the task in the Asana UI. Confirm, in Asana: a successor exists dated two days out, it carries `repeat:2d` and the original's other tags, it sits in the section the original was in, the original has lost its `repeat:` tag, and the original has a comment linking to the successor.
- [ ] Un-complete the original, **re-add the `repeat:2d` tag** to it (completion stripped it), then re-complete it. Confirm **no second successor** is created and the CF logs show `Recurrence for … already created as … — skipping`. (Un-complete/re-complete *without* re-adding the tag proves nothing — `find_rule` returns `None` and `spawn_next` is never called, so the external-gid guard, and the `external:recur:<gid>` lookup it depends on, are never exercised.)
- [ ] Confirm the `external:recur:<gid>` lookup shape works against real Asana at all: this is the one path no unit test exercises (every test stubs `find_task_by_external`), and it's a colon inside an external id, a shape no existing caller (`external:{message_id}`, a UUID) has ever sent. The previous step's "skipping" log line is exactly this — if it doesn't appear, or Asana 400s the lookup, that's a real recurrence bug, not a test gap.
- [ ] Check the CF logs with the `fetch-tasks-logs` skill for the `Recurring task … → … due …` line and no exceptions.
- [ ] Delete the test tasks and the `repeat:2d` workspace tag.

Then open the PR with the `/pr-open` skill.

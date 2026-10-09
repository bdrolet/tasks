# Strategy Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the prioritizer a goal layer: parse a written strategy, judge what each task serves, rank necessary work first, evaluate lead measures and tripwires daily, and run a weekly review.

**Architecture:** A pure parser turns the `## Strategy` section of the standing context into `Goal` objects. The existing enrichment and gate-2 calls gain the strategy as a cached system block and three output fields. The pure scorer gains a `necessity` term behind a `mode` switch that ships as `flag`. The daily tick evaluates goal state, fires tripwires as tasks, and the API exposes a review. Every piece follows the repo's layer rules: `models/` pure types, `services/` pure logic, `repo/` SQL only, `handlers/` orchestration, `api/routers/` thin transport.

**Tech Stack:** Python 3.13, pydantic, FastAPI, psycopg/pg8000 via `clients/db.py`, Anthropic SDK (`claude-opus-5-5` enrichment, `claude-sonnet-5` gate 2), pytest, Terraform.

**Spec:** `docs/superpowers/specs/2026-10-09-strategy-layer-design.md`

## Global Constraints

- Layer rules from `CLAUDE.md`: `services/` has no I/O; `repo/` takes an open connection and never opens one; `handlers/` are called only from `main.py`; `models/` import nothing from other layers.
- The subscriber (`handlers/prioritize.py`) raises on DB or Asana failure and never on a model failure (prioritizer D7); an unreadable strategy is not a failure (spec D13).
- Necessity `mode` ships as `flag`; in `flag` the scorer's output must equal today's output to the digit (spec D6).
- Precedence for every enrichable field: tag > override > model > default (spec D4).
- No calendar ids, facts or goal content are committed to this repo; `context/standing-context.example.md` holds placeholders only (spec D2).
- Model ids are exactly `claude-opus-5-5` (enrichment) and `claude-sonnet-5` (gate 2, unchanged). No date suffixes.
- Run tests with `.venv/bin/pytest tests/ -q`; commit after every task; never commit to `main` (branch `strategy-layer-design` already exists; implementation goes on `strategy-layer`).

## Review Focus

1. **A task tagged `serves:` for a goal id that was later renamed or removed in the document.** Expected: the tag is ignored for scoring (necessity falls to `unattached`), the review lists the task under grooming with "unknown goal". Test pinned in Task 4.
2. **A tripwire whose `by` date is edited after it has fired.** Expected: the external id includes the `by` date, so an edit creates a second task only if it fires again under the new date, never a duplicate for the old one. Test pinned in Task 9.
3. **Gate 2 returning `serves` with a confidence but the strategy section empty (feature off).** Expected: creation path ignores the judgment entirely, writes no tags, suppresses nothing. Test pinned in Task 12.
4. **`overdue` on a task that is overdue but snoozed.** Expected: not counted; the signal stays false. Test pinned in Task 7.
5. **The same `task_changed` message delivered twice after a `serves:` write-back.** Expected: one claim, one Asana tag write, one comment. Test pinned in Task 6.

## File map

| File | Role |
|---|---|
| `models/strategy.py` | **new** pure types: `Measure`, `Tripwire`, `Signal`, `Goal`, `Strategy`, `GoalState` |
| `services/strategy.py` | **new** pure parser `parse(text)`, `load()` over the standing-context section, `text_hash` |
| `services/goal_state.py` | **new** pure daily evaluation: leads, lag, tripwires, signals with debounce, next step, diagnosis |
| `services/review.py` | **new** pure: build the review dict from rows; render it as markdown |
| `services/prioritize_config.py` | `[necessity]`, `[strategy]`, `weights.necessity` |
| `services/prioritize.py` | `serves`/`role` precedence, necessity term, `stop_doing` bucket, goal-horizon due, boost, flag renormalisation |
| `services/enrichment.py` | strategy system block, three schema fields, attach comment, `strategy_hash` in the cache key |
| `services/triage.py` | strategy system block, three schema fields |
| `clients/claude.py` | `extract_structured` / `run_agent` accept a list of system blocks; refusal fallback on `extract_structured` |
| `models/prioritize.py` | `Enrichment.serves`, `necessity_confidence`, `necessity_reason`; `TaskFacts.serves_estimated` |
| `models/events.py` | `Decision.serves`, `necessity_confidence`, `necessity_reason` |
| `repo/prioritize.py` | `serves_estimated` column + `claim_serves`, `set_tags`, `strategy_hash` on enrichment, calibration rows for necessity |
| `repo/goals.py` | **new** `goal_state`, `goal_reports`, `goal_overrides` |
| `repo/tasks.py` | `serves_estimated` on the pipeline row |
| `repo/suppressions.py` | `web_link`, `restored_at`, `restored_task_gid`; list necessity suppressions; mark restored |
| `repo/schema.sql` | new tables and columns |
| `handlers/prioritize.py` | serves write-back; day_changed: evaluate → tripwires → rescore → next steps |
| `handlers/task_create.py` | tags at creation; necessity suppression; `create_from_event` split out for restore |
| `handlers/weekly_review.py` | **new** `run()`: build review, upsert the standing task, post the comment |
| `api/routers/review.py` | **new** `GET /review`, `POST /goals/{id}/reports`, `POST /goals/{id}/mute`, `POST /suppressions/{message_id}/restore` |
| `api/routers/next.py` | `/calibrate` necessity section |
| `api/main.py`, `main.py` | router + `/review` route |
| `config/prioritize.toml` | new sections |
| `terraform/cloud_functions.tf`, `terraform/scheduler.tf` | mount on `tasks-prioritize`; `tasks-weekly-review` job |
| `context/standing-context.example.md` | `## Strategy` example |
| `.claude/agents/task-next.md`, `.claude/agents/task-builder.md`, `.claude/skills/prioritizing-tasks/SKILL.md`, `scripts/task_next.py` | `review`, `report`, `mute`; tagging rule |
| `scripts/test-review.py` | **new** dry-run render against the live DB |
| `CLAUDE.md` | strategy layer paragraph |

Branch: `git checkout -b strategy-layer main` (the spec branch is merged first or cherry-picked; the plan assumes the spec is on `main`).

---

### Task 1: Strategy types and parser

**Files:**
- Create: `models/strategy.py`
- Create: `services/strategy.py`
- Test: `tests/test_strategy.py`

**Interfaces:**
- Produces: `models.strategy.Measure(tag: str, op: str, value: float, period: str)`; `Tripwire(ordinal: int, subject: str, op: str, value: float, by: date, action: str)`; `Signal(kind: str, cls: str, text: str, min_priority: str = "P1", grace: int = 3, tag: str | None = None, after: date | None = None, days: int | None = None, value: float | None = None, period: str | None = None)`; `Goal(id, kind, weight, horizon, lag, leads, tripwires, standard, signals, review, prose)`; `Strategy(goals, last_reviewed, findings, text_hash)` with `Strategy.EMPTY`, `.get(id)`, `.outcome_goals()`, `.areas()`; `services.strategy.parse(text: str, *, stale_after_days: int = 90, today: date | None = None) -> Strategy`; `services.strategy.load(*, stale_after_days: int = 90, today: date | None = None) -> Strategy`; `services.strategy.text_hash(text: str) -> str`.

- [ ] **Step 1: Write the failing parser tests**

```python
# tests/test_strategy.py
from datetime import date

from models.strategy import Strategy
from services import standing_context
from services import strategy as st

TODAY = date(2026, 10, 9)
DOC = """- last reviewed: 2026-10-01

### consulting
- kind: outcome
- weight: 1.0
- horizon: 2027-03-31
- lag: consulting revenue >= 15000 per month
- lead: conversation >= 3 per week
- lead: proposal >= 2 per month
- tripwire: signed-client = 0 by 2026-12-31 -> Revisit consulting niche and offer
- tripwire: lag < 5000 by 2027-01-31 -> Revisit pricing

**Diagnosis.** Nobody knows I exist.
**Guiding policy.** Referrals only. Not doing: cold email.

### finances
- kind: area
- weight: 0.9
- standard: bills paid on time
- below-the-line: overdue; undated:tax after 2026-10-31; stale > 35 days; lead bill < 1 per month; overdue:P2+ grace 7

**Standard.** Enough money.
"""


def test_parse_outcome_goal_header_and_prose():
    s = st.parse(DOC, today=TODAY)
    assert s.last_reviewed == date(2026, 10, 1)
    g = s.get("consulting")
    assert g.kind == "outcome" and g.weight == 1.0 and g.horizon == date(2027, 3, 31)
    assert g.lag.tag == "consulting revenue" and g.lag.op == ">=" and g.lag.value == 15000
    assert g.lag.period == "month"
    assert [(m.tag, m.op, m.value, m.period) for m in g.leads] == [
        ("conversation", ">=", 3.0, "week"),
        ("proposal", ">=", 2.0, "month"),
    ]
    t1, t2 = g.tripwires
    assert (t1.ordinal, t1.subject, t1.op, t1.value, t1.by) == (1, "signed-client", "=", 0.0, date(2026, 12, 31))
    assert t1.action == "Revisit consulting niche and offer"
    assert t2.subject == "lag" and t2.action == "Revisit pricing"
    assert "**Diagnosis.** Nobody knows I exist." in g.prose
    assert "- kind:" not in g.prose


def test_parse_area_signals():
    g = st.parse(DOC, today=TODAY).get("finances")
    assert g.kind == "area" and g.standard == "bills paid on time"
    kinds = [(x.kind, x.cls) for x in g.signals]
    assert kinds == [
        ("overdue", "evidence"),
        ("undated", "evidence"),
        ("stale", "absence"),
        ("lead", "absence"),
        ("overdue", "evidence"),
    ]
    overdue_default, undated, stale, lead, overdue_wide = g.signals
    assert overdue_default.min_priority == "P1" and overdue_default.grace == 3
    assert undated.tag == "tax" and undated.after == date(2026, 10, 31)
    assert stale.days == 35
    assert (lead.tag, lead.value, lead.period) == ("bill", 1.0, "month")
    assert overdue_wide.min_priority == "P2" and overdue_wide.grace == 7


def test_malformed_block_is_skipped_with_a_finding():
    doc = DOC + "\n### broken\n- weight: 2\n\nprose\n\n### alsobroken\n- kind: sideways\n"
    s = st.parse(doc, today=TODAY)
    assert {g.id for g in s.goals} == {"consulting", "finances"}
    assert any("broken: missing kind" in f for f in s.findings)
    assert any("alsobroken: unknown kind" in f for f in s.findings)


def test_bad_header_line_is_a_finding_not_a_failure():
    doc = DOC.replace("- lead: proposal >= 2 per month", "- lead: proposal twice weekly")
    s = st.parse(doc, today=TODAY)
    assert s.get("consulting") is not None
    assert len(s.get("consulting").leads) == 1
    assert any("consulting: lead" in f for f in s.findings)


def test_duplicate_id_keeps_first():
    doc = DOC + "\n### consulting\n- kind: area\n- standard: x\n"
    s = st.parse(doc, today=TODAY)
    assert s.get("consulting").kind == "outcome"
    assert any("duplicate id consulting" in f for f in s.findings)


def test_empty_text_is_empty_strategy():
    assert st.parse("", today=TODAY) == Strategy.EMPTY
    assert st.parse("   \n", today=TODAY).goals == ()


def test_review_findings_for_missing_tripwire_lead_and_stale_doc():
    doc = "- last reviewed: 2026-01-01\n\n### biz\n- kind: outcome\n- horizon: 2027-01-01\n"
    s = st.parse(doc, today=TODAY, stale_after_days=90)
    assert "biz: outcome goal has no tripwire" in s.findings
    assert "biz: outcome goal has no lead measure" in s.findings
    assert any(f.startswith("strategy last reviewed 281 days ago") for f in s.findings)


def test_unknown_signal_is_ignored_with_a_finding():
    doc = "### home\n- kind: area\n- below-the-line: dishes > 3; stale > 14 days\n"
    s = st.parse(doc, today=TODAY)
    assert [x.kind for x in s.get("home").signals] == ["stale"]
    assert any("home: below-the-line" in f for f in s.findings)


def test_text_hash_is_stable_and_changes_with_text():
    assert st.text_hash(DOC) == st.text_hash(DOC)
    assert st.text_hash(DOC) != st.text_hash(DOC + "x")
    assert st.parse(DOC, today=TODAY).text_hash == st.text_hash(DOC)


def test_load_reads_the_strategy_section(monkeypatch):
    monkeypatch.setattr(
        standing_context, "section", lambda name, **kw: DOC if name == "Strategy" else ""
    )
    s = st.load(today=TODAY)
    assert {g.id for g in s.goals} == {"consulting", "finances"}


def test_load_without_section_is_empty(monkeypatch):
    monkeypatch.setattr(standing_context, "section", lambda name, **kw: "")
    assert st.load(today=TODAY) == Strategy.EMPTY
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_strategy.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'models.strategy'`

- [ ] **Step 3: Write the types**

```python
# models/strategy.py
"""Pure types for the strategy layer. No imports from other layers.
Design: docs/superpowers/specs/2026-10-09-strategy-layer-design.md"""

from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

ROLES = ("path", "derisk", "support")
KINDS = ("outcome", "area")
PERIODS = ("day", "week", "month")
OPS = (">=", "<=", "=", "<", ">")
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


@dataclass(frozen=True)
class Measure:
    """A `lead:` or `lag:` line. For a lead, `tag` is an Asana tag name; for
    a lag it is the free text before the operator."""

    tag: str
    op: str
    value: float
    period: str


@dataclass(frozen=True)
class Tripwire:
    ordinal: int  # 1-based position among the block's tripwire lines
    subject: str  # a lead tag name, or the literal "lag"
    op: str
    value: float
    by: date
    action: str


@dataclass(frozen=True)
class Signal:
    """One below-the-line signal. `cls` is "evidence" (overdue, undated) or
    "absence" (stale, lead) — only evidence signals may boost scoring."""

    kind: str  # overdue | undated | stale | lead
    cls: str
    text: str
    min_priority: str = "P1"
    grace: int = 3
    tag: str | None = None
    after: date | None = None
    days: int | None = None
    value: float | None = None
    period: str | None = None


@dataclass(frozen=True)
class Goal:
    id: str
    kind: str
    weight: float = 1.0
    horizon: date | None = None
    lag: Measure | None = None
    leads: tuple[Measure, ...] = ()
    tripwires: tuple[Tripwire, ...] = ()
    standard: str | None = None
    signals: tuple[Signal, ...] = ()
    review: str = "weekly"
    prose: str = ""


@dataclass(frozen=True)
class Strategy:
    goals: tuple[Goal, ...] = ()
    last_reviewed: date | None = None
    findings: tuple[str, ...] = ()
    text_hash: str = ""

    EMPTY: ClassVar["Strategy"]

    def get(self, goal_id: str) -> Goal | None:
        return next((g for g in self.goals if g.id == goal_id), None)

    def outcome_goals(self) -> tuple[Goal, ...]:
        return tuple(g for g in self.goals if g.kind == "outcome")

    def areas(self) -> tuple[Goal, ...]:
        return tuple(g for g in self.goals if g.kind == "area")


Strategy.EMPTY = Strategy()


@dataclass(frozen=True)
class SignalState:
    signal: str
    cls: str
    raw: bool | None  # None = no_data
    effective: bool
    consecutive_days: int
    tasks: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if self.raw is None:
            return "no_data"
        return "true" if self.effective else "false"


@dataclass(frozen=True)
class GoalState:
    goal_id: str
    kind: str
    day: date
    strategy_hash: str
    state: dict = field(default_factory=dict)
```

- [ ] **Step 4: Write the parser**

```python
# services/strategy.py
"""Parse the `## Strategy` section of the standing context into goals and
areas. Pure: `parse` takes text; `load` reads the section through
services.standing_context. Lenient by design — a bad block or line is a
finding, never an exception (spec D3).
Design: docs/superpowers/specs/2026-10-09-strategy-layer-design.md (D2, D3, D9)"""

import hashlib
import logging
import re
from datetime import date

from models.strategy import (
    KINDS,
    OPS,
    PERIODS,
    Goal,
    Measure,
    Signal,
    Strategy,
    Tripwire,
)
from services import standing_context

logger = logging.getLogger(__name__)

SECTION = "Strategy"
_ID_RE = re.compile(r"^[a-z0-9-]+$")
_HEADER_RE = re.compile(r"^- ([a-z][a-z -]*?):\s*(.*)$")
_OPS = "|".join(re.escape(o) for o in sorted(OPS, key=len, reverse=True))
_NUM = r"(\d+(?:\.\d+)?)"
_DATE = r"(\d{4}-\d{2}-\d{2})"
_LEAD_RE = re.compile(rf"^(\S+)\s*({_OPS})\s*{_NUM}\s+per\s+(day|week|month)$")
_LAG_RE = re.compile(rf"^(.+?)\s*({_OPS})\s*{_NUM}\s+per\s+(day|week|month)$")
_TRIP_RE = re.compile(rf"^(\S+)\s*({_OPS})\s*{_NUM}\s+by\s+{_DATE}\s*->\s*(.+)$")
_OVERDUE_RE = re.compile(r"^overdue(?::P([0-3])\+)?(?:\s+grace\s+(\d+))?$")
_OVERDUE_TAG_RE = re.compile(r"^overdue:([a-z0-9][a-z0-9_-]*)(?:\s+grace\s+(\d+))?$")
_UNDATED_RE = re.compile(rf"^undated:(\S+)\s+after\s+{_DATE}$")
_STALE_RE = re.compile(r"^stale\s*>\s*(\d+)\s+days?$")
_LEAD_SIG_RE = re.compile(rf"^lead\s+(\S+)\s*<\s*{_NUM}\s+per\s+(day|week|month)$")


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_signal(text: str) -> Signal | None:
    t = text.strip()
    if m := _OVERDUE_RE.match(t):
        return Signal(
            kind="overdue",
            cls="evidence",
            text=t,
            min_priority=f"P{m.group(1)}" if m.group(1) else "P1",
            grace=int(m.group(2)) if m.group(2) else 3,
        )
    if m := _OVERDUE_TAG_RE.match(t):
        return Signal(
            kind="overdue",
            cls="evidence",
            text=t,
            min_priority="P3",  # a tag signal applies no priority floor
            grace=int(m.group(2)) if m.group(2) else 3,
            tag=m.group(1),
        )
    if m := _UNDATED_RE.match(t):
        return Signal(kind="undated", cls="evidence", text=t, tag=m.group(1), after=_date(m.group(2)))
    if m := _STALE_RE.match(t):
        return Signal(kind="stale", cls="absence", text=t, days=int(m.group(1)))
    if m := _LEAD_SIG_RE.match(t):
        return Signal(
            kind="lead", cls="absence", text=t, tag=m.group(1), value=float(m.group(2)), period=m.group(3)
        )
    return None


def _split_blocks(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(preamble lines, [(id, block body)])."""
    preamble: list[str] = []
    blocks: list[tuple[str, str]] = []
    current_id: str | None = None
    current: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("### "):
            if current_id is not None:
                blocks.append((current_id, "\n".join(current)))
            current_id, current = line[4:].strip(), []
        elif current_id is None:
            preamble.append(line)
        else:
            current.append(line)
    if current_id is not None:
        blocks.append((current_id, "\n".join(current)))
    return preamble, blocks


def _parse_block(goal_id: str, body: str, findings: list[str]) -> Goal | None:
    header: list[tuple[str, str]] = []
    prose_lines: list[str] = []
    in_header = True
    for line in body.splitlines():
        m = _HEADER_RE.match(line) if in_header else None
        if m:
            header.append((m.group(1).strip(), m.group(2).strip()))
        elif in_header and not line.strip() and not header:
            continue
        else:
            in_header = False
            prose_lines.append(line)
    fields: dict = {"id": goal_id, "prose": "\n".join(prose_lines).strip()}
    leads: list[Measure] = []
    tripwires: list[Tripwire] = []
    signals: list[Signal] = []
    for key, value in header:
        try:
            if key == "kind":
                if value not in KINDS:
                    findings.append(f"{goal_id}: unknown kind {value!r}")
                    return None
                fields["kind"] = value
            elif key == "weight":
                fields["weight"] = float(value)
            elif key == "horizon":
                fields["horizon"] = _date(value)
            elif key == "lag":
                m = _LAG_RE.match(value)
                if not m:
                    raise ValueError(value)
                fields["lag"] = Measure(m.group(1).strip(), m.group(2), float(m.group(3)), m.group(4))
            elif key == "lead":
                m = _LEAD_RE.match(value)
                if not m:
                    raise ValueError(value)
                leads.append(Measure(m.group(1), m.group(2), float(m.group(3)), m.group(4)))
            elif key == "tripwire":
                m = _TRIP_RE.match(value)
                if not m:
                    raise ValueError(value)
                tripwires.append(
                    Tripwire(
                        ordinal=len(tripwires) + 1,
                        subject=m.group(1),
                        op=m.group(2),
                        value=float(m.group(3)),
                        by=_date(m.group(4)),
                        action=m.group(5).strip(),
                    )
                )
            elif key == "standard":
                fields["standard"] = value
            elif key == "below-the-line":
                for part in value.split(";"):
                    sig = _parse_signal(part)
                    if sig is None:
                        findings.append(f"{goal_id}: below-the-line signal {part.strip()!r} not understood")
                    else:
                        signals.append(sig)
            elif key == "review":
                if value not in ("weekly", "monthly"):
                    raise ValueError(value)
                fields["review"] = value
            else:
                findings.append(f"{goal_id}: unknown header {key!r}")
        except ValueError:
            findings.append(f"{goal_id}: {key} line {value!r} not understood")
    if "kind" not in fields:
        findings.append(f"{goal_id}: missing kind")
        return None
    return Goal(leads=tuple(leads), tripwires=tuple(tripwires), signals=tuple(signals), **fields)


def parse(text: str, *, stale_after_days: int = 90, today: date | None = None) -> Strategy:
    if not (text or "").strip():
        return Strategy.EMPTY
    today = today or date.today()
    findings: list[str] = []
    preamble, blocks = _split_blocks(text)
    last_reviewed: date | None = None
    for line in preamble:
        m = _HEADER_RE.match(line)
        if m and m.group(1) == "last reviewed":
            try:
                last_reviewed = _date(m.group(2))
            except ValueError:
                findings.append(f"last reviewed {m.group(2)!r} not understood")
    goals: list[Goal] = []
    seen: set[str] = set()
    for goal_id, body in blocks:
        if not _ID_RE.match(goal_id):
            findings.append(f"{goal_id}: id must match [a-z0-9-]+")
            continue
        if goal_id in seen:
            findings.append(f"duplicate id {goal_id}; keeping the first")
            continue
        g = _parse_block(goal_id, body, findings)
        if g is None:
            continue
        seen.add(goal_id)
        goals.append(g)
    for g in goals:
        if g.kind == "outcome" and not g.tripwires:
            findings.append(f"{g.id}: outcome goal has no tripwire")
        if g.kind == "outcome" and not g.leads:
            findings.append(f"{g.id}: outcome goal has no lead measure")
    if last_reviewed is None:
        findings.append("strategy has no 'last reviewed' line")
    elif (today - last_reviewed).days > stale_after_days:
        findings.append(f"strategy last reviewed {(today - last_reviewed).days} days ago")
    for f in findings:
        logger.warning("strategy: %s", f)
    return Strategy(
        goals=tuple(goals),
        last_reviewed=last_reviewed,
        findings=tuple(findings),
        text_hash=text_hash(text),
    )


def section_text() -> str:
    return standing_context.section(SECTION)


def load(*, stale_after_days: int = 90, today: date | None = None) -> Strategy:
    """The loaded strategy, or Strategy.EMPTY when the section is absent —
    every consumer treats EMPTY as "necessity is neutral" (spec D3)."""
    return parse(section_text(), stale_after_days=stale_after_days, today=today)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_strategy.py -q`
Expected: 11 passed. If `test_parse_outcome_goal_header_and_prose` fails on the prose assertion, check that `_parse_block` stops reading header lines at the first non-`- key:` line and that the blank line between header and prose is dropped by `.strip()`.

- [ ] **Step 6: Commit**

```bash
git add models/strategy.py services/strategy.py tests/test_strategy.py
git commit -m "feat(strategy): types and lenient parser for the ## Strategy section"
```

---

### Task 2: Config — necessity, strategy, weights

**Files:**
- Modify: `config/prioritize.toml`
- Modify: `services/prioritize_config.py`
- Test: `tests/test_prioritize_config.py`

**Interfaces:**
- Produces on `Config`: `necessity_mode: str`, `necessity_role: dict[str, float]`, `necessity_unattached: float`, `below_the_line_boost: float`, `strategy_stale_after_days: int`, `lag_flat_periods: int`, `suppression_settle_days: int`, `signal_debounce_days: int`, `min_tagged_for_signals: int`; `weights["necessity"]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_prioritize_config.py
import pytest

from services import prioritize_config as pc


def test_weights_still_sum_to_one_with_necessity():
    cfg = pc.load()
    assert abs(sum(cfg.weights.values()) - 1.0) < 1e-9
    assert cfg.weights["necessity"] == 0.20


def test_necessity_and_strategy_sections():
    cfg = pc.load()
    assert cfg.necessity_mode == "flag"
    assert cfg.necessity_role == {"path": 1.0, "derisk": 0.9, "support": 0.5}
    assert cfg.necessity_unattached == 0.2
    assert cfg.below_the_line_boost == 1.15
    assert cfg.strategy_stale_after_days == 90
    assert cfg.lag_flat_periods == 2
    assert cfg.suppression_settle_days == 30
    assert cfg.signal_debounce_days == 3
    assert cfg.min_tagged_for_signals == 5


def test_bad_mode_is_rejected(tmp_path):
    text = pc.DEFAULT_PATH.read_text().replace('mode = "flag"', 'mode = "yolo"')
    p = tmp_path / "p.toml"
    p.write_text(text)
    with pytest.raises(ValueError, match="necessity.mode"):
        pc.load(str(p))
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize_config.py -q`
Expected: FAIL with `KeyError: 'necessity'` and `AttributeError: 'Config' object has no attribute 'necessity_mode'`

- [ ] **Step 3: Edit the TOML**

Replace the `[weights]` block and append two sections to `config/prioritize.toml`:

```toml
[weights]                     # cost_of_delay terms; sum to 1.0
priority = 0.20               # each of the six is the pre-strategy value × 0.8; flag mode divides back
urgency = 0.24
impact = 0.12
unblock = 0.08
aging = 0.04
category = 0.12
necessity = 0.20
```

```toml
[necessity]                   # spec D6
mode = "flag"                 # flag | demote | suppress
path = 1.0
derisk = 0.9
support = 0.5
unattached = 0.2              # serves empty with low confidence; unenriched; no strategy loaded
below_the_line_boost = 1.15   # cost-of-delay multiplier for a slipping area's path/derisk tasks (evidence signals only)

[strategy]                    # spec D3, D9, D11, D14
stale_after_days = 90         # 'last reviewed' older than this is a review finding
lag_flat_periods = 2          # consecutive unmet lag periods, with leads met, before "lead strong, lag flat"
suppression_settle_days = 30  # a necessity suppression not restored by then counts as agreement
signal_debounce_days = 3      # consecutive raw-true days before a signal flips, and raw-false before it clears
min_tagged_for_signals = 5    # an area with fewer serves:<area> tasks reports no_data for every signal
```

- [ ] **Step 4: Extend `Config` and `load`**

In `services/prioritize_config.py`, add the fields after `starvation_max_boost` (before `fingerprint`):

```python
    necessity_mode: str = "flag"
    necessity_role: dict[str, float] = None  # type: ignore[assignment]
    necessity_unattached: float = 0.2
    below_the_line_boost: float = 1.15
    strategy_stale_after_days: int = 90
    lag_flat_periods: int = 2
    suppression_settle_days: int = 30
    signal_debounce_days: int = 3
    min_tagged_for_signals: int = 5
```

and in `load`, after `starve = raw["starvation"]`:

```python
    nec, strat = raw["necessity"], raw["strategy"]
    if nec["mode"] not in ("flag", "demote", "suppress"):
        raise ValueError(f"necessity.mode must be flag|demote|suppress, got {nec['mode']!r}")
```

and in the `Config(...)` call, before `fingerprint=`:

```python
        necessity_mode=str(nec["mode"]),
        necessity_role={k: float(nec[k]) for k in ("path", "derisk", "support")},
        necessity_unattached=float(nec["unattached"]),
        below_the_line_boost=float(nec["below_the_line_boost"]),
        strategy_stale_after_days=int(strat["stale_after_days"]),
        lag_flat_periods=int(strat["lag_flat_periods"]),
        suppression_settle_days=int(strat["suppression_settle_days"]),
        signal_debounce_days=int(strat["signal_debounce_days"]),
        min_tagged_for_signals=int(strat["min_tagged_for_signals"]),
```

- [ ] **Step 5: Run the config tests and the full scorer tests**

Run: `.venv/bin/pytest tests/test_prioritize_config.py tests/test_prioritize.py -q`
Expected: config tests pass. `tests/test_prioritize.py` still passes because the scorer does not yet read `weights["necessity"]` — the `cod` sum simply omits it; Task 4 wires it in and adds the exactness test.

- [ ] **Step 6: Commit**

```bash
git add config/prioritize.toml services/prioritize_config.py tests/test_prioritize_config.py
git commit -m "feat(strategy): necessity and strategy config sections; weights scaled for flag mode"
```

---

### Task 3: Enrichment judges necessity

**Files:**
- Modify: `models/prioritize.py` (`Enrichment`, `TaskFacts`)
- Modify: `services/enrichment.py`
- Modify: `clients/claude.py:88-125` (`extract_structured`)
- Test: `tests/test_enrichment.py`, `tests/test_claude_extract.py`, `tests/test_models_prioritize.py`

**Interfaces:**
- Consumes: `services.strategy.text_hash`.
- Produces: `models.prioritize.Serve(goal: str, role: str, confidence: str)`; `Enrichment.serves: tuple[Serve, ...]`, `Enrichment.necessity_confidence: str`, `Enrichment.necessity_reason: str | None`; `TaskFacts.serves_estimated: dict | None`; `enrichment.content_hash(name, notes, comments, strategy_hash="")`; `enrichment.extract(..., strategy_text: str = "", known_goals: tuple[str, ...] = ())`; `enrichment.is_service_comment(text)` (the old `is_estimate_comment` name stays as an alias); `enrichment.attach_comment(goal_ids: list[str], role: str) -> str`; `enrichment.system_blocks(strategy_text) -> list[dict]`; `clients.claude.extract_structured(system: str | list[dict], ...)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_enrichment.py
GOOD_SERVES = GOOD | {
    "serves": [{"goal": "consulting", "role": "path", "confidence": "high"}],
    "necessity_confidence": "high",
    "necessity_reason": "it is the next path step",
}


def test_parse_reads_serves():
    e = en.parse(json.dumps(GOOD_SERVES), known_goals=("consulting",))
    assert e.serves == (en.Serve("consulting", "path", "high"),)
    assert e.necessity_confidence == "high" and e.necessity_reason == "it is the next path step"


def test_parse_drops_unknown_goal_ids():
    raw = GOOD_SERVES | {"serves": [{"goal": "ghost", "role": "path", "confidence": "high"}]}
    e = en.parse(json.dumps(raw), known_goals=("consulting",))
    assert e.serves == ()


def test_parse_without_serves_fields_defaults_to_low_none():
    e = en.parse(json.dumps(GOOD), known_goals=())
    assert e.serves == () and e.necessity_confidence == "low" and e.necessity_reason is None


def test_content_hash_changes_with_strategy_hash():
    assert en.content_hash("n", "notes", COMMENTS, strategy_hash="a") != en.content_hash(
        "n", "notes", COMMENTS, strategy_hash="b"
    )
    assert en.content_hash("n", "notes", COMMENTS) == en.content_hash("n", "notes", COMMENTS, strategy_hash="")


def test_attach_comment_is_excluded_from_the_hash():
    attach = {"text": en.attach_comment(["consulting"], "path"), "created_by": "tasks", "created_at": "x"}
    assert en.content_hash("n", "notes", COMMENTS + [attach]) == en.content_hash("n", "notes", COMMENTS)
    assert en.is_service_comment(attach["text"]) and en.is_estimate_comment("Estimated 3 points — adjust if wrong.")


def test_system_blocks_put_strategy_second_and_cached():
    blocks = en.system_blocks("### consulting\n- kind: outcome\n")
    assert blocks[0]["text"] == en.SYSTEM_PROMPT and blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"].startswith("## Strategy") and "### consulting" in blocks[1]["text"]
    assert blocks[1]["cache_control"] == {"type": "ephemeral"}
    assert en.system_blocks("") == [blocks[0]]


def test_extract_passes_strategy_blocks_and_model(monkeypatch):
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return json.dumps(GOOD_SERVES)

    e = en.extract(
        name="n", project="p", html_notes="<body>x</body>", comments=[], due_on=None,
        start_on=None, tags=[], today=date(2026, 10, 9), strategy_text="### consulting\n- kind: outcome\n",
        known_goals=("consulting",), call=fake,
    )
    assert seen["model"] == "claude-opus-5-5" and seen["effort"] == "medium"
    assert isinstance(seen["system"], list) and len(seen["system"]) == 2
    assert e.serves[0].goal == "consulting"
```

```python
# append to tests/test_claude_extract.py
def test_extract_structured_accepts_system_blocks(monkeypatch):
    captured = {}

    class Msg:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": "{}"})()]
        usage = type("U", (), {"input_tokens": 1, "output_tokens": 1})()

    class Beta:
        class messages:
            @staticmethod
            def create(**kw):
                captured.update(kw)
                return Msg()

    class Client:
        beta = Beta()

    monkeypatch.setattr(claude, "_get_client", lambda: Client())
    blocks = [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "b"}]
    claude.extract_structured(model="claude-opus-5-5", system=blocks, user="u", schema={"type": "object"}, effort="medium")
    assert captured["system"] == blocks
    assert captured["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in captured["betas"]
    assert captured["output_config"]["effort"] == "medium"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_enrichment.py tests/test_claude_extract.py -q`
Expected: FAIL — `AttributeError: module 'services.enrichment' has no attribute 'Serve'`, and the client test fails on `captured["system"]` being a one-element list.

- [ ] **Step 3: Extend the models**

In `models/prioritize.py` add after the imports:

```python
@dataclass(frozen=True)
class Serve:
    goal: str
    role: str  # path | derisk | support
    confidence: str  # low | medium | high
```

Add to `TaskFacts` after `points_estimated`:

```python
    serves_estimated: dict | None = None  # the serves draft, once; NULL = never judged for write-back
```

(`TaskFacts` fields after it have no defaults, so move `serves_estimated` to the END of the class instead, after `content_hash`, with the default shown.)

Add to `Enrichment` after `reason`:

```python
    serves: tuple[Serve, ...] = ()
    necessity_confidence: str = "low"
    necessity_reason: str | None = None
```

(`unenriched: bool` has no default and sits before these; reorder so `unenriched` stays without a default by giving these three defaults and placing them after `unenriched`.) Update `Enrichment.DEFAULT` to pass `serves=(), necessity_confidence="low", necessity_reason=None`.

- [ ] **Step 4: Extend the enrichment service**

In `services/enrichment.py`:

```python
from models.prioritize import Enrichment, Serve
from models.strategy import ROLES

MODEL = "claude-opus-5-5"
EFFORT = "medium"
ATTACH_COMMENT_PREFIX = "Attached to "
ATTACH_COMMENT_SUFFIX = " — adjust the tags if wrong."
```

Schema additions inside `SCHEMA["properties"]`:

```python
        "serves": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "goal": {"type": "string"},
                    "role": {"type": "string", "enum": ["path", "derisk", "support"]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["goal", "role", "confidence"],
            },
        },
        "necessity_confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "necessity_reason": {"type": "string"},
```

and add `"serves", "necessity_confidence", "necessity_reason"` to `SCHEMA["required"]`.

Append to `SYSTEM_PROMPT`:

```python
SYSTEM_PROMPT += """

If a ## Strategy section follows, judge what this task serves. serves — a list of {goal, role, confidence}; goal is a ### id from the strategy; role is path (a precondition on the goal's written path, or the obvious next step toward one), derisk (its absence puts the outcome or the area's standard at significant risk) or support (helps, but the goal is reachable without it). An empty list means the task serves no goal or area; with high confidence that is a real and useful answer, not a failure. necessity_confidence — how sure you are of the serves list as a whole, including an empty one. necessity_reason — one sentence. If no ## Strategy section follows, return an empty serves list with low confidence."""
```

New helpers (replace `is_estimate_comment`, keep its name as an alias):

```python
def is_service_comment(text: str | None) -> bool:
    """Comments this service posts: the points estimate and the attach note.
    Both are excluded from the content hash so a write-back cannot re-trigger
    enrichment (D5, D6)."""
    if text is None:
        return False
    return (text.startswith(ESTIMATE_COMMENT_PREFIX) and text.endswith(ESTIMATE_COMMENT_SUFFIX)) or (
        text.startswith(ATTACH_COMMENT_PREFIX) and text.endswith(ATTACH_COMMENT_SUFFIX)
    )


is_estimate_comment = is_service_comment


def attach_comment(goal_ids: list[str], role: str) -> str:
    return f"{ATTACH_COMMENT_PREFIX}{', '.join(goal_ids)} as {role}{ATTACH_COMMENT_SUFFIX}"


def system_blocks(strategy_text: str) -> list[dict]:
    """Static instructions first, the strategy second, both cached — the
    strategy is identical for every task, so a full re-judge reads it from
    cache (spec §Model calls)."""
    blocks = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    if strategy_text.strip():
        blocks.append(
            {
                "type": "text",
                "text": "## Strategy\n\n" + strategy_text.strip(),
                "cache_control": {"type": "ephemeral"},
            }
        )
    return blocks
```

`_comment_lines` calls `is_service_comment`. `content_hash` becomes:

```python
def content_hash(name: str, notes: str, comments: list[dict], strategy_hash: str = "") -> str:
    body = "\n".join([name or "", notes or "", *_comment_lines(comments)])
    if strategy_hash:
        body += "\n" + strategy_hash
    return hashlib.sha256(body.encode()).hexdigest()
```

`_Out` gains:

```python
class _ServeOut(BaseModel):
    goal: str
    role: Literal["path", "derisk", "support"]
    confidence: Literal["low", "medium", "high"]


class _Out(BaseModel):
    ...
    serves: list[_ServeOut] = []
    necessity_confidence: Literal["low", "medium", "high"] = "low"
    necessity_reason: str | None = None
```

`parse` gains `known_goals`:

```python
def parse(raw: str, *, known_goals: tuple[str, ...] = ()) -> Enrichment:
    ...
    serves = tuple(
        Serve(s.goal, s.role, s.confidence)
        for s in data.serves
        if s.goal in known_goals and s.role in ROLES
    )
    return Enrichment(
        ...,
        serves=serves,
        necessity_confidence=data.necessity_confidence,
        necessity_reason=(data.necessity_reason or None),
        unenriched=False,
    )
```

`extract` gains `strategy_text: str = ""` and `known_goals: tuple[str, ...] = ()`; it calls `call(model=MODEL, system=system_blocks(strategy_text), user=..., schema=SCHEMA, effort=EFFORT)` and returns `parse(raw, known_goals=known_goals)`.

- [ ] **Step 5: Extend the Claude client**

In `clients/claude.py`, change `extract_structured`'s signature to `system: str | list[dict]` and its body to:

```python
    blocks = (
        [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if isinstance(system, str)
        else system
    )
    response = _get_client().beta.messages.create(  # type: ignore[call-overload]
        model=model,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        system=blocks,
        messages=[{"role": "user", "content": user}],
        # A safety-classifier decline is re-run on a fallback model inside the
        # same call, so a refusal degrades to a judgment rather than an
        # unenriched row (spec §Model calls). Array-form header would be a 400.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
```

Keep the `stop_reason != "end_turn"` guard: a fallback that itself refuses still raises. Update the docstring: "Opus 5.5 runs adaptive thinking always; effort is the only depth control and defaults to medium there, so callers pass it explicitly."

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_enrichment.py tests/test_claude_extract.py tests/test_models_prioritize.py tests/test_prioritize_handler.py -q`
Expected: all pass. `test_prioritize_handler.py` still passes because `facts_from` calls `content_hash` with three args and `extract` is faked.

- [ ] **Step 7: Commit**

```bash
git add models/prioritize.py services/enrichment.py clients/claude.py tests/test_enrichment.py tests/test_claude_extract.py
git commit -m "feat(strategy): enrichment judges serves/role; strategy as cached system block; Opus 5.5"
```

---

### Task 4: Scorer — necessity term, stop_doing bucket, goal horizon, boost, flag renormalisation

**Files:**
- Modify: `services/prioritize.py`
- Test: `tests/test_prioritize.py`

**Interfaces:**
- Consumes: `Config` fields from Task 2; `Enrichment.serves` from Task 3; `models.strategy.Strategy`, `Goal`.
- Produces: `score_set(facts, enrichments, overrides, stats, config, today, project_last_offered=None, *, strategy: Strategy = Strategy.EMPTY, below_the_line: frozenset[str] = frozenset())`; `Effective.serves: tuple[str, ...]`, `Effective.role: str | None`, `Effective.necessity_source: str`, `Effective.necessity_confidence: str`; `_TAG_FIELDS` gains `"role"`; `effective()` reads repeatable `serves:` tags; components gain `necessity`, `serves`, `role`, `necessity_source`, `necessity_confidence`, `grooming`, `below_the_line`, `N`; bucket value `stop_doing`; `due_source` value `goal_horizon`; `effective_weights(config) -> dict[str, float]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_prioritize.py
from dataclasses import replace as _replace
from datetime import date as _date

from models.prioritize import Serve
from models.strategy import Goal, Strategy

STRATEGY = Strategy(
    goals=(
        Goal(id="consulting", kind="outcome", weight=1.0, horizon=_date(2026, 12, 31)),
        Goal(id="finances", kind="area", weight=0.8),
    )
)


def cfg_with(**kw):
    return _replace(CFG, **kw)


def run_s(fs, enrichments=None, overrides=None, config=None, below=frozenset(), today=TODAY):
    return pz.score_set(
        fs, enrichments or {}, overrides or {}, {}, config or CFG, today,
        strategy=STRATEGY, below_the_line=below,
    )


def test_flag_mode_reproduces_pre_strategy_scores_exactly():
    old = cfg_with(weights={"priority": 0.25, "urgency": 0.30, "impact": 0.15, "unblock": 0.10, "aging": 0.05, "category": 0.15})
    fs = [facts("a", due_on=TODAY + timedelta(days=5), points=1), facts("b", name="[P3] b", points=5), facts("c", project="Consulting")]
    before = {t.gid: t.score for t in pz.score_set(fs, {}, {}, {}, old, TODAY).tasks}
    after = {t.gid: t.score for t in run_s(fs, config=cfg_with(necessity_mode="flag")).tasks}
    for gid in before:
        assert abs(before[gid] - after[gid]) < 1e-12, gid


def test_effective_weights_flag_vs_demote():
    w = pz.effective_weights(cfg_with(necessity_mode="flag"))
    assert w["necessity"] == 0 and abs(w["priority"] - 0.25) < 1e-12
    w = pz.effective_weights(cfg_with(necessity_mode="demote"))
    assert w == CFG.weights


def test_necessity_from_tags_wins_over_model():
    f = facts("a", tags=("serves:finances", "role:support"))
    e = enr(serves=(Serve("consulting", "path", "high"),), necessity_confidence="high")
    c = run_s([f], {"a": e}, config=cfg_with(necessity_mode="demote")).by_gid()["a"].components
    assert c["serves"] == ["finances"] and c["role"] == "support" and c["necessity_source"] == "tag"
    assert abs(c["N"] - 0.8 * 0.5) < 1e-12


def test_necessity_roles_and_weights():
    for role, expected in (("path", 1.0), ("derisk", 0.9), ("support", 0.5)):
        f = facts("a", tags=("serves:consulting", f"role:{role}"))
        c = run_s([f], config=cfg_with(necessity_mode="demote")).by_gid()["a"].components
        assert abs(c["N"] - expected) < 1e-12, role


def test_model_serves_without_tags_uses_highest_role():
    e = enr(serves=(Serve("finances", "support", "high"), Serve("consulting", "derisk", "medium")), necessity_confidence="high")
    c = run_s([facts("a")], {"a": e}, config=cfg_with(necessity_mode="demote")).by_gid()["a"].components
    assert c["role"] == "derisk" and c["necessity_source"] == "model" and abs(c["N"] - 0.9) < 1e-12


def test_unknown_goal_tag_is_unattached_and_groomed():
    f = facts("a", tags=("serves:ghost", "role:path"))
    c = run_s([f], config=cfg_with(necessity_mode="demote")).by_gid()["a"].components
    assert c["serves"] == [] and c["N"] == CFG.necessity_unattached and c["grooming"] is True


def test_uncertain_none_is_unattached_and_groomed_not_suppressed():
    e = enr(serves=(), necessity_confidence="low")
    t = run_s([facts("a")], {"a": e}, config=cfg_with(necessity_mode="suppress")).by_gid()["a"]
    assert t.bucket == "next" and t.components["grooming"] is True
    assert t.components["N"] == CFG.necessity_unattached


def test_confident_none_is_stop_doing_only_in_suppress_mode():
    e = enr(serves=(), necessity_confidence="high")
    for mode, bucket in (("flag", "next"), ("demote", "next"), ("suppress", "stop_doing")):
        t = run_s([facts("a")], {"a": e}, config=cfg_with(necessity_mode=mode)).by_gid()["a"]
        assert t.bucket == bucket, mode


def test_pin_overrides_stop_doing():
    e = enr(serves=(), necessity_confidence="high")
    ov = {"a": Overrides(pinned_rank=1)}
    t = run_s([facts("a")], {"a": e}, ov, config=cfg_with(necessity_mode="suppress")).by_gid()["a"]
    assert t.bucket == "next" and t.components["pinned_despite"] == "stop_doing"


def test_goal_horizon_becomes_soft_due_for_path_tasks_without_dates():
    f = facts("a", name="[P3] a", tags=("serves:consulting", "role:path"))  # P3 horizon = +120d from TS
    c = run_s([f]).by_gid()["a"].components
    assert c["effective_due"] == "2026-12-31" and c["due_source"] == "goal_horizon" and c["soft"]


def test_goal_horizon_does_not_replace_a_nearer_priority_horizon_or_any_real_date():
    f = facts("a", name="[P0] a", tags=("serves:consulting", "role:path"))  # P0 horizon = +3d
    assert run_s([f]).by_gid()["a"].components["due_source"] == "horizon"
    f = facts("b", due_on=TODAY + timedelta(days=100), tags=("serves:consulting", "role:path"))
    assert run_s([f]).by_gid()["b"].components["due_source"] == "hard"
    f = facts("c", tags=("serves:consulting", "role:support"))
    assert run_s([f]).by_gid()["c"].components["due_source"] == "horizon"


def test_below_the_line_boosts_path_and_derisk_only_in_demote():
    cfg = cfg_with(necessity_mode="demote")
    path = facts("a", tags=("serves:finances", "role:path"))
    support = facts("b", tags=("serves:finances", "role:support"))
    plain = run_s([path, support], config=cfg).by_gid()
    boosted = run_s([path, support], config=cfg, below=frozenset({"finances"})).by_gid()
    assert abs(boosted["a"].score / plain["a"].score - CFG.below_the_line_boost) < 1e-9
    assert abs(boosted["b"].score - plain["b"].score) < 1e-12
    assert boosted["a"].components["below_the_line"] is True


def test_no_strategy_means_everything_unattached_and_equal():
    e = enr(serves=(Serve("consulting", "path", "high"),), necessity_confidence="high")
    s = pz.score_set([facts("a"), facts("b")], {"a": e}, {}, {}, cfg_with(necessity_mode="demote"), TODAY)
    c = s.by_gid()
    assert c["a"].components["N"] == c["b"].components["N"] == CFG.necessity_unattached
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize.py -q -k "necessity or flag_mode or stop_doing or goal_horizon or below_the_line or no_strategy or effective_weights"`
Expected: FAIL with `TypeError: score_set() got an unexpected keyword argument 'strategy'`

- [ ] **Step 3: Implement**

In `services/prioritize.py`:

```python
from models.strategy import Goal, Strategy

_TAG_FIELDS = ("waiting", "energy", "impact", "role")
ROLE_RANK = {"path": 3, "derisk": 2, "support": 1}
```

Extend `Effective`:

```python
    serves: tuple[str, ...] = ()
    role: str | None = None
    necessity_source: str = "default"  # tag | override | model | default
    necessity_confidence: str = "low"
```

Add a serves-tag reader beside `_tag_values`:

```python
def _serves_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    out = []
    for tag in tags:
        key, sep, value = tag.partition(":")
        if sep and key.strip().casefold() == "serves" and value.strip():
            out.append(value.strip())
    return tuple(out)
```

`effective(facts, enrichment, overrides, config, strategy: Strategy = Strategy.EMPTY)` — after the existing picks:

```python
    known = {g.id for g in strategy.goals}
    tag_serves = tuple(s for s in _serves_tags(facts.tags) if s in known)
    tag_role = tags.get("role") if tags.get("role") in ROLE_RANK else None
    if _serves_tags(facts.tags) or tag_role:
        serves, role, source = tag_serves, tag_role or "support", "tag"
    elif o.get("serves") is not None:
        serves = tuple(s for s in o["serves"] if s in known)
        role, source = (o.get("role") if o.get("role") in ROLE_RANK else "support"), "override"
    elif enrichment.serves:
        ok = [s for s in enrichment.serves if s.goal in known]
        serves = tuple(s.goal for s in ok)
        role = max((s.role for s in ok), key=lambda r: ROLE_RANK[r], default=None)
        source = "model"
    else:
        serves, role, source = (), None, "model" if not enrichment.unenriched else "default"
    if not strategy.goals:
        serves, role, source = (), None, "default"
```

and pass `serves=serves, role=role, necessity_source=source, necessity_confidence=enrichment.necessity_confidence` into `Effective(...)`. A tag naming an unknown goal yields empty `serves` with source `tag` — that is the grooming case the review surfaces as "unknown goal".

Necessity, as a module function:

```python
def necessity(eff: Effective, strategy: Strategy, config: Config) -> tuple[float, bool]:
    """(N, grooming). Unattached when nothing known is served; grooming when
    the judgment is uncertain or a tag names an unknown goal."""
    if not strategy.goals or not eff.serves:
        uncertain = eff.necessity_source in ("model", "default") and eff.necessity_confidence == "low"
        groomed = uncertain or (eff.necessity_source == "tag")  # a tag with no known goal
        return config.necessity_unattached, groomed
    role_factor = config.necessity_role[eff.role or "support"]
    weight = max(strategy.get(g).weight for g in eff.serves if strategy.get(g))
    return weight * role_factor, False


def effective_weights(config: Config) -> dict[str, float]:
    """flag mode: drop the necessity term and divide the rest by (1 - w),
    which recovers the pre-strategy weights to the digit (spec D6)."""
    w = dict(config.weights)
    n = w.pop("necessity", 0.0)
    if config.necessity_mode == "flag":
        return {k: v / (1.0 - n) for k, v in w.items()} | {"necessity": 0.0}
    return config.weights
```

`_effective_due(facts, eff, config, ancestors, strategy)` — insert before the priority horizon:

```python
    horizon = config.horizon_days.get(facts.priority or config.default_priority)
    prio_due = _local_date(facts.created_at) + timedelta(days=horizon) if horizon is not None else None
    if eff.role in ("path", "derisk"):
        goal_dues = [g.horizon for g in (strategy.get(s) for s in eff.serves) if g and g.kind == "outcome" and g.horizon]
        if goal_dues:
            goal_due = min(goal_dues)
            if prio_due is None or goal_due < prio_due:
                return goal_due, "goal_horizon", None
    if prio_due is None:
        return None, "none", None
    return prio_due, "horizon", None
```

`caps` in `score_set` gains `"goal_horizon": config.soft_cap_horizon`.

`score_set` signature gains `*, strategy: Strategy = Strategy.EMPTY, below_the_line: frozenset[str] = frozenset()`. The `prepared` loop passes `strategy` to `effective`. After `_bucket` (which stays as is), add the stop-doing rule:

```python
        n_val, grooming = necessity(eff, strategy, config)
        confident_none = (
            strategy.goals
            and not eff.serves
            and eff.necessity_source == "model"
            and eff.necessity_confidence in ("medium", "high")
        )
        if confident_none and bucket == "next" and config.necessity_mode == "suppress":
            if ov.pinned_rank is not None:
                despite = despite or "stop_doing"
            else:
                bucket = "stop_doing"
```

Components gain:

```python
                "necessity": n_val,
                "N": n_val,
                "serves": list(eff.serves),
                "role": eff.role,
                "necessity_source": eff.necessity_source,
                "necessity_confidence": eff.necessity_confidence,
                "grooming": grooming,
                "confident_none": bool(confident_none),
                "below_the_line": bool(
                    eff.role in ("path", "derisk") and any(s in below_the_line for s in eff.serves)
                ),
```

In the scoring loop, replace the `w = config.weights` block:

```python
        w = effective_weights(config)
        cod = (
            w["priority"] * p_weight
            + w["urgency"] * u
            + w["impact"] * i
            + w["unblock"] * b
            + w["aging"] * a
            + w["category"] * cat
            + w["necessity"] * c["N"]
        )
        if c["below_the_line"] and config.necessity_mode != "flag":
            cod *= config.below_the_line_boost
```

`side_lists` gains `"grooming"` (active tasks with `components["grooming"]`) and `"stop_doing"` (bucket `stop_doing` plus active tasks with `components["confident_none"]`, so flag and demote modes still list them).

- [ ] **Step 4: Run the whole scorer suite**

Run: `.venv/bin/pytest tests/test_prioritize.py tests/test_api_next.py -q`
Expected: all pass, including the pre-existing fixtures (flag mode is exact).

- [ ] **Step 5: Commit**

```bash
git add services/prioritize.py tests/test_prioritize.py
git commit -m "feat(strategy): necessity term, stop_doing bucket, goal horizon, below-the-line boost (flag mode exact)"
```

---

### Task 5: Schema and repo — serves claim, goal tables, suppression columns

**Files:**
- Modify: `repo/schema.sql`
- Modify: `repo/prioritize.py`
- Create: `repo/goals.py`
- Modify: `repo/tasks.py`, `repo/suppressions.py`
- Test: `tests/test_repo_prioritize.py`, `tests/test_repo_goals.py` (new), `tests/test_repo.py`, `tests/test_repo_suppressions.py`

**Interfaces:**
- Produces: `repo.goals.save_snapshot(conn, strategy: Strategy)`, `repo.goals.load_snapshot(conn) -> Strategy` (goals carry id, kind, weight, horizon only); `repo.prioritize.claim_serves(conn, gid, payload: dict) -> bool`; `repo.prioritize.set_tags(conn, gid, tags: list[str]) -> None`; `repo.prioritize.upsert_enrichment(conn, gid, content_hash, raw, model, strategy_hash: str = "")`; `repo.prioritize.necessity_rows(conn) -> list[dict]` (`task_gid, serves_estimated, tags, overrides`); `repo.goals.upsert_state(conn, state: GoalState)`, `get_states(conn, day) -> dict[str, GoalState]`, `insert_report(conn, goal_id, value, period_start) -> int`, `latest_reports(conn, goal_id, limit=3) -> list[dict]`, `all_latest_reports(conn) -> dict[str, list[dict]]`, `get_mutes(conn) -> dict[str, date]`, `set_mute(conn, goal_id, until: date | None)`, `set_next_steps(conn, day, steps: dict[str, str | None])`; `repo.tasks.insert(..., serves_estimated: dict | None = None)`; `repo.suppressions.insert(..., web_link: str | None = None)`, `list_necessity(conn, *, limit=100) -> list[dict]`, `get(conn, message_id) -> dict | None`, `mark_restored(conn, message_id, task_gid) -> bool`, `restore_rates(conn, settle_days) -> list[dict]`.

- [ ] **Step 1: Write the failing repo tests**

```python
# tests/test_repo_goals.py
from datetime import date

from models.strategy import GoalState
from repo import goals as repo
from tests.test_repo import FakeConn


def test_upsert_state_writes_json():
    conn = FakeConn()
    repo.upsert_state(conn, GoalState("consulting", "outcome", date(2026, 10, 9), "h", {"a": 1}))
    q, p = conn.executed[0]
    assert "INSERT INTO goal_state" in q and "ON CONFLICT (goal_id, day)" in q
    assert p[0] == "consulting" and p[1] == date(2026, 10, 9) and '"a": 1' in p[4]


def test_set_mute_clears_with_none():
    conn = FakeConn()
    repo.set_mute(conn, "finances", None)
    q, p = conn.executed[0]
    assert "INSERT INTO goal_overrides" in q and p == ("finances", None)


def test_insert_report_returns_id():
    conn = FakeConn(row={"id": 7})
    assert repo.insert_report(conn, "consulting", 4200.0, date(2026, 10, 1)) == 7


def test_snapshot_round_trip():
    from models.strategy import Goal, Strategy
    conn = FakeConn()
    strat = Strategy(goals=(Goal(id="consulting", kind="outcome", weight=1.0, horizon=date(2027, 3, 31)),),
                     last_reviewed=date(2026, 10, 1), findings=("x",), text_hash="h")
    repo.save_snapshot(conn, strat)
    q, p = conn.executed[0]
    assert "INSERT INTO strategy_snapshot" in q and p[0] == "h" and '"horizon": "2027-03-31"' in p[3]
    row = {"text_hash": "h", "last_reviewed": date(2026, 10, 1), "findings": ["x"],
           "goals": [{"id": "consulting", "kind": "outcome", "weight": 1.0, "horizon": "2027-03-31"}]}
    loaded = repo.load_snapshot(FakeConn(row=row))
    assert loaded.get("consulting").horizon == date(2027, 3, 31) and loaded.findings == ("x",)
    assert repo.load_snapshot(FakeConn(row=None)) == Strategy.EMPTY
```

```python
# append to tests/test_repo_prioritize.py
from repo import prioritize as repo
from tests.test_repo import FakeConn


def test_claim_serves_is_conditional_on_null():
    conn = FakeConn()
    repo.claim_serves(conn, "t1", {"serves": ["consulting"], "role": "path"})
    q, p = conn.executed[0]
    assert "SET serves_estimated = %s" in q and "serves_estimated IS NULL" in q
    assert p[1] == "t1" and '"role": "path"' in p[0]


def test_set_tags_bumps_fetched_at():
    conn = FakeConn()
    repo.set_tags(conn, "t1", ["serves:consulting", "role:path"])
    q, p = conn.executed[0]
    assert "UPDATE task_facts SET tags = %s" in q and "fetched_at = now()" in q


def test_upsert_enrichment_stores_strategy_hash():
    conn = FakeConn()
    repo.upsert_enrichment(conn, "t1", "h", {"x": 1}, "m", strategy_hash="s")
    q, p = conn.executed[0]
    assert "strategy_hash" in q and p[-1] == "s"
```

```python
# append to tests/test_repo_suppressions.py
from repo import suppressions as repo
from tests.test_repo import FakeConn


def test_insert_stores_web_link():
    conn = FakeConn()
    repo.insert(conn, message_id="m", category="c", importance="P2", subject="s", sender="x",
                reason="r", source="necessity", related_task_gid=None, evidence=[], web_link="https://x")
    q, p = conn.executed[0]
    assert "web_link" in q and p[-1] == "https://x"


def test_mark_restored_is_conditional():
    conn = FakeConn()
    repo.mark_restored(conn, "m", "t9")
    q, p = conn.executed[0]
    assert "restored_at IS NULL" in q and p == ("t9", "m")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_repo_goals.py tests/test_repo_prioritize.py tests/test_repo_suppressions.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'repo.goals'`, `AttributeError: ... has no attribute 'claim_serves'`.

- [ ] **Step 3: Schema**

Append to `repo/schema.sql`:

```sql
-- Strategy layer (docs/superpowers/specs/2026-10-09-strategy-layer-design.md)
ALTER TABLE task_facts ADD COLUMN IF NOT EXISTS serves_estimated JSONB;        -- D5: NULL = never judged for write-back
ALTER TABLE task_enrichment ADD COLUMN IF NOT EXISTS strategy_hash TEXT;       -- D4
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS serves_estimated JSONB;             -- D14: gate-2 draft at creation
ALTER TABLE suppressed_emails ADD COLUMN IF NOT EXISTS web_link TEXT;          -- D14: the restore link
ALTER TABLE suppressed_emails ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
ALTER TABLE suppressed_emails ADD COLUMN IF NOT EXISTS restored_task_gid TEXT;

CREATE TABLE IF NOT EXISTS goal_reports (
    id            BIGSERIAL PRIMARY KEY,
    goal_id       TEXT NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    period_start  DATE,
    reported_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS goal_reports_goal_idx ON goal_reports (goal_id, reported_at DESC);

CREATE TABLE IF NOT EXISTS goal_overrides (
    goal_id       TEXT PRIMARY KEY,
    mute_until    DATE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The parsed strategy as the daily tick last saw it, so the tasks-api and
-- the webhook CF read goals from the database and never mount the secret
-- (spec D1: only tasks-prioritize mounts it). One row.
CREATE TABLE IF NOT EXISTS strategy_snapshot (
    id            INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    text_hash     TEXT NOT NULL,
    last_reviewed DATE,
    findings      JSONB NOT NULL DEFAULT '[]',
    goals         JSONB NOT NULL DEFAULT '[]',   -- [{id, kind, weight, horizon}]
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per goal per day: leads, lag, tripwires, signals (raw, effective,
-- consecutive_days, state, tasks), below_the_line, muted_until, next_step,
-- diagnosis. Written by the daily tick (D7).
CREATE TABLE IF NOT EXISTS goal_state (
    goal_id       TEXT NOT NULL,
    day           DATE NOT NULL,
    kind          TEXT NOT NULL,
    state         JSONB NOT NULL,
    strategy_hash TEXT NOT NULL,
    PRIMARY KEY (goal_id, day)
);
```

- [ ] **Step 4: `repo/prioritize.py`**

Add `serves_estimated` to `_FACT_COLS` (at the end) and to `_row_to_facts` (`serves_estimated=_as_json(r["serves_estimated"], None)`), and to `upsert_facts`'s column list and VALUES (one more `%s`, value `json.dumps(f.serves_estimated) if f.serves_estimated is not None else None`) — but NOT to the `ON CONFLICT ... UPDATE SET` list, with the same comment as `points_estimated`. Add:

```python
def claim_serves(conn: Any, gid: str, payload: dict) -> bool:
    """Spec D5: the conditional claim whose rowcount decides who writes."""
    cur = conn.execute(
        "UPDATE task_facts SET serves_estimated = %s WHERE task_gid = %s AND serves_estimated IS NULL",
        (json.dumps(payload), gid),
    )
    return cur.rowcount == 1


def set_tags(conn: Any, gid: str, tags: list[str]) -> None:
    """Record tags just written to Asana (same rationale as set_story_points)."""
    conn.execute(
        "UPDATE task_facts SET tags = %s, fetched_at = now() WHERE task_gid = %s",
        (json.dumps(tags), gid),
    )


def necessity_rows(conn: Any) -> list[dict]:
    return conn.execute(
        """
        SELECT f.task_gid, f.serves_estimated, f.tags, o.overrides
        FROM task_facts f LEFT JOIN task_overrides o USING (task_gid)
        WHERE f.serves_estimated IS NOT NULL
        """
    ).fetchall()
```

`upsert_enrichment` gains `strategy_hash: str = ""` and writes the column in both INSERT and UPDATE.

- [ ] **Step 5: `repo/goals.py`**

```python
"""goal_state, goal_reports, goal_overrides — the strategy layer's three
tables. Takes an open connection."""

import json
from datetime import date
from typing import Any

from models.strategy import Goal, GoalState, Strategy


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def upsert_state(conn: Any, state: GoalState) -> None:
    conn.execute(
        """
        INSERT INTO goal_state (goal_id, day, kind, strategy_hash, state)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (goal_id, day) DO UPDATE SET
            kind = EXCLUDED.kind, strategy_hash = EXCLUDED.strategy_hash, state = EXCLUDED.state
        """,
        (state.goal_id, state.day, state.kind, state.strategy_hash, json.dumps(state.state)),
    )


def get_states(conn: Any, day: date) -> dict[str, GoalState]:
    rows = conn.execute(
        "SELECT goal_id, day, kind, strategy_hash, state FROM goal_state WHERE day = %s", (day,)
    ).fetchall()
    return {
        r["goal_id"]: GoalState(r["goal_id"], r["kind"], r["day"], r["strategy_hash"], _as_json(r["state"], {}))
        for r in rows
    }


def set_next_steps(conn: Any, day: date, steps: dict[str, str | None]) -> None:
    for goal_id, gid in steps.items():
        conn.execute(
            """
            UPDATE goal_state SET state = state || %s::jsonb WHERE goal_id = %s AND day = %s
            """,
            (json.dumps({"next_step": gid, "stalled": gid is None}), goal_id, day),
        )


def insert_report(conn: Any, goal_id: str, value: float, period_start: date | None) -> int:
    row = conn.execute(
        "INSERT INTO goal_reports (goal_id, value, period_start) VALUES (%s, %s, %s) RETURNING id",
        (goal_id, value, period_start),
    ).fetchone()
    return int(row["id"])


def latest_reports(conn: Any, goal_id: str, limit: int = 3) -> list[dict]:
    return conn.execute(
        "SELECT value, period_start, reported_at FROM goal_reports WHERE goal_id = %s "
        "ORDER BY reported_at DESC LIMIT %s",
        (goal_id, limit),
    ).fetchall()


def all_latest_reports(conn: Any, limit: int = 3) -> dict[str, list[dict]]:
    rows = conn.execute(
        "SELECT goal_id, value, period_start, reported_at FROM goal_reports ORDER BY reported_at DESC"
    ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        bucket = out.setdefault(r["goal_id"], [])
        if len(bucket) < limit:
            bucket.append(r)
    return out


def save_snapshot(conn: Any, strategy: Strategy) -> None:
    goals = [
        {"id": g.id, "kind": g.kind, "weight": g.weight, "horizon": g.horizon.isoformat() if g.horizon else None}
        for g in strategy.goals
    ]
    conn.execute(
        """
        INSERT INTO strategy_snapshot (id, text_hash, last_reviewed, findings, goals, updated_at)
        VALUES (1, %s, %s, %s, %s, now())
        ON CONFLICT (id) DO UPDATE SET text_hash = EXCLUDED.text_hash,
            last_reviewed = EXCLUDED.last_reviewed, findings = EXCLUDED.findings,
            goals = EXCLUDED.goals, updated_at = now()
        """,
        (strategy.text_hash, strategy.last_reviewed, json.dumps(list(strategy.findings)), json.dumps(goals)),
    )


def load_snapshot(conn: Any) -> Strategy:
    row = conn.execute(
        "SELECT text_hash, last_reviewed, findings, goals FROM strategy_snapshot WHERE id = 1"
    ).fetchone()
    if row is None:
        return Strategy.EMPTY
    goals = tuple(
        Goal(id=g["id"], kind=g["kind"], weight=float(g.get("weight") or 1.0),
             horizon=date.fromisoformat(g["horizon"]) if g.get("horizon") else None)
        for g in _as_json(row["goals"], [])
    )
    lr = row["last_reviewed"]
    return Strategy(goals=goals, last_reviewed=lr if isinstance(lr, date) or lr is None else date.fromisoformat(str(lr)),
                    findings=tuple(_as_json(row["findings"], [])), text_hash=row["text_hash"])


def get_mutes(conn: Any) -> dict[str, date]:
    rows = conn.execute("SELECT goal_id, mute_until FROM goal_overrides WHERE mute_until IS NOT NULL").fetchall()
    return {r["goal_id"]: r["mute_until"] for r in rows}


def set_mute(conn: Any, goal_id: str, until: date | None) -> None:
    conn.execute(
        """
        INSERT INTO goal_overrides (goal_id, mute_until) VALUES (%s, %s)
        ON CONFLICT (goal_id) DO UPDATE SET mute_until = EXCLUDED.mute_until, updated_at = now()
        """,
        (goal_id, until),
    )
```

- [ ] **Step 6: `repo/tasks.py` and `repo/suppressions.py`**

`repo.tasks.insert` gains `serves_estimated: dict | None = None` and writes `serves_estimated` as `json.dumps(...)` or `None` (one more column, `%s`).

`repo.suppressions.insert` gains `web_link: str | None = None` as the last column. Add:

```python
def get(conn: Any, message_id: str) -> dict | None:
    return conn.execute(
        "SELECT message_id, category, importance, subject, sender, reason, source, web_link, "
        "restored_at, restored_task_gid, created_at FROM suppressed_emails WHERE message_id = %s",
        (message_id,),
    ).fetchone()


def list_necessity(conn: Any, *, limit: int = 100) -> list[dict]:
    return conn.execute(
        "SELECT message_id, subject, sender, reason, web_link, created_at, restored_at, restored_task_gid "
        "FROM suppressed_emails WHERE source = 'necessity' ORDER BY created_at DESC LIMIT %s",
        (limit,),
    ).fetchall()


def mark_restored(conn: Any, message_id: str, task_gid: str) -> bool:
    cur = conn.execute(
        "UPDATE suppressed_emails SET restored_at = now(), restored_task_gid = %s "
        "WHERE message_id = %s AND restored_at IS NULL",
        (task_gid, message_id),
    )
    return cur.rowcount == 1


def restore_rates(conn: Any, settle_days: int) -> list[dict]:
    """Per confidence band stored in evidence[0].necessity_confidence (D14):
    restored count, settled-unrestored count, pending count."""
    return conn.execute(
        """
        SELECT COALESCE(evidence->0->>'necessity_confidence', 'unknown') AS band,
               COUNT(*) FILTER (WHERE restored_at IS NOT NULL) AS restored,
               COUNT(*) FILTER (WHERE restored_at IS NULL AND created_at < now() - make_interval(days => %s)) AS settled,
               COUNT(*) FILTER (WHERE restored_at IS NULL AND created_at >= now() - make_interval(days => %s)) AS pending
        FROM suppressed_emails WHERE source = 'necessity' GROUP BY 1
        """,
        (settle_days, settle_days),
    ).fetchall()
```

- [ ] **Step 7: Run the repo tests and the handler suite**

Run: `.venv/bin/pytest tests/test_repo_goals.py tests/test_repo_prioritize.py tests/test_repo_suppressions.py tests/test_repo.py tests/test_prioritize_handler.py tests/test_task_create.py -q`
Expected: all pass (the handler fixtures fake the repo functions by name, and the new keyword arguments default).

- [ ] **Step 8: Commit**

```bash
git add repo/schema.sql repo/prioritize.py repo/goals.py repo/tasks.py repo/suppressions.py tests/test_repo_goals.py tests/test_repo_prioritize.py tests/test_repo_suppressions.py
git commit -m "feat(strategy): schema and repo — serves claim, goal tables, suppression restore columns"
```

---

### Task 6: Subscriber — strategy into enrichment, serves write-back, rescore with strategy

**Files:**
- Modify: `handlers/prioritize.py`
- Test: `tests/test_prioritize_handler.py`

**Interfaces:**
- Consumes: `services.strategy.load`, `enrichment.extract(strategy_text=, known_goals=)`, `repo.prioritize.claim_serves`, `repo.prioritize.set_tags`, `services.tags.resolve_gids`, `asana.add_tag`, `enrichment.attach_comment`, `repo.goals.get_states`.
- Produces: `handlers.prioritize.claim_serves_write_back(conn, facts, enrichment, config) -> dict | None`; `write_back_serves(gid, payload: dict) -> list[str] | None` (the tag names written); `facts_from(..., strategy_hash: str = "")`; `rescore(conn, ..., strategy=None)` reading today's `goal_state` for `below_the_line`.

- [ ] **Step 1: Write the failing handler tests**

```python
# append to tests/test_prioritize_handler.py
from models.strategy import Goal, Strategy
from repo import goals as repo_goals
from services import strategy as st
from services import tags as tags_service

STRAT = Strategy(goals=(Goal(id="consulting", kind="outcome"), Goal(id="finances", kind="area")), text_hash="sh")
SERVES_HIGH = GOOD | {
    "serves": [{"goal": "consulting", "role": "path", "confidence": "high"}],
    "necessity_confidence": "high", "necessity_reason": "next step",
}


@pytest.fixture
def strategy(monkeypatch):
    monkeypatch.setattr(st, "load", lambda **kw: STRAT)
    monkeypatch.setattr(st, "section_text", lambda: "### consulting\n- kind: outcome\n")
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {})
    return STRAT


@pytest.fixture
def tag_fake(monkeypatch, asana_fake):
    asana_fake["tags"] = []
    monkeypatch.setattr(tags_service, "resolve_gids", lambda names: [f"g-{n}" for n in names])
    monkeypatch.setattr(asana, "add_tag", lambda gid, tag_gid: asana_fake["tags"].append((gid, tag_gid)))
    monkeypatch.setattr(repo, "claim_serves", lambda c, gid, payload: c.__dict__.setdefault("serves_claims", {}).setdefault(gid, payload) is payload)
    monkeypatch.setattr(repo, "set_tags", lambda c, gid, tags: c.__dict__.setdefault("tags_set", []).append((gid, tags)))
    return asana_fake


def _model_returning(monkeypatch, payload):
    calls = []

    def fake(**kw):
        calls.append(kw)
        return json.dumps(payload)

    monkeypatch.setattr(en, "extract", lambda **kw: en.parse(fake(**kw), known_goals=kw.get("known_goals", ())))
    return calls


def test_enrichment_receives_strategy_text_and_hash_in_facts(db, tag_fake, strategy, monkeypatch):
    calls = _model_returning(monkeypatch, SERVES_HIGH)
    h.handle_task_changed("t1", today=TODAY)
    assert calls[0]["strategy_text"].startswith("### consulting") and calls[0]["known_goals"] == ("consulting", "finances")
    assert db.facts["t1"].content_hash == en.content_hash(TASK["name"], TASK["notes"], [
        {"text": "sent it", "created_by": "Ben", "created_at": "2026-09-19T00:00:00Z"}], strategy_hash="sh")


def test_confident_serves_is_written_back_once(db, tag_fake, strategy, monkeypatch):
    _model_returning(monkeypatch, SERVES_HIGH)
    h.handle_task_changed("t1", today=TODAY)
    h.handle_task_changed("t1", today=TODAY)  # redelivery
    assert tag_fake["tags"] == [("t1", "g-serves:consulting"), ("t1", "g-role:path")]
    assert [c for c in tag_fake["comments"] if c.startswith("Attached to")] == [
        "Attached to consulting as path — adjust the tags if wrong."
    ]
    assert db.tags_set == [("t1", ["cheryl", "serves:consulting", "role:path"])]
    assert db.serves_claims["t1"] == {"serves": ["consulting"], "role": "path", "confidence": "high"}


def test_low_confidence_claims_grooming_and_writes_nothing(db, tag_fake, strategy, monkeypatch):
    _model_returning(monkeypatch, SERVES_HIGH | {"necessity_confidence": "low"})
    h.handle_task_changed("t1", today=TODAY)
    assert tag_fake["tags"] == [] and db.serves_claims["t1"] == {"grooming": True}


def test_confident_none_claims_none_and_writes_nothing(db, tag_fake, strategy, monkeypatch):
    _model_returning(monkeypatch, SERVES_HIGH | {"serves": []})
    h.handle_task_changed("t1", today=TODAY)
    assert tag_fake["tags"] == [] and db.serves_claims["t1"] == {"none": True, "confidence": "high"}


def test_existing_serves_tag_skips_the_draft(db, tag_fake, strategy, monkeypatch):
    _model_returning(monkeypatch, SERVES_HIGH)
    monkeypatch.setitem(TASK, "tags", [{"gid": "g", "name": "serves:finances"}])
    try:
        h.handle_task_changed("t1", today=TODAY)
    finally:
        TASK["tags"] = [{"gid": "g", "name": "cheryl"}]
    assert tag_fake["tags"] == [] and "serves_claims" not in db.__dict__


def test_no_strategy_means_no_claim(db, tag_fake, monkeypatch):
    monkeypatch.setattr(st, "load", lambda **kw: Strategy.EMPTY)
    monkeypatch.setattr(st, "section_text", lambda: "")
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {})
    _model_returning(monkeypatch, SERVES_HIGH)
    h.handle_task_changed("t1", today=TODAY)
    assert "serves_claims" not in db.__dict__


def test_tag_write_failure_keeps_the_claim(db, tag_fake, strategy, monkeypatch):
    _model_returning(monkeypatch, SERVES_HIGH)

    def boom(gid, tag_gid):
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "add_tag", boom)
    h.handle_task_changed("t1", today=TODAY)
    assert db.serves_claims["t1"]["serves"] == ["consulting"] and not db.__dict__.get("tags_set")
```

Also update the existing `db` fixture's `MemConn.__init__` to set `self.goal_states = {}`, and make the `model` fixture call `en.parse(fake(**kw), known_goals=kw.get("known_goals", ()))`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize_handler.py -q -k "serves or strategy or no_strategy"`
Expected: FAIL — `extract` is called without `strategy_text`, `claim_serves` never invoked.

- [ ] **Step 3: Implement in `handlers/prioritize.py`**

Imports:

```python
from models.strategy import Strategy
from repo import goals as repo_goals
from services import strategy as strategy_service
from services import tags as tags_service
```

`facts_from(..., strategy_hash: str = "")` passes it to `en.content_hash(name, notes, comments, strategy_hash=strategy_hash)`. `gather(gid, strategy_hash="")` threads it through `facts_from` and `_gather_subtasks` (add the same keyword there).

`enrich_one(conn, facts, raw_task, comments, today, strategy)`:

```python
    try:
        enrichment = en.extract(
            ...,
            today=today,
            strategy_text=strategy_service.section_text() if strategy.goals else "",
            known_goals=tuple(g.id for g in strategy.goals),
        )
    ...
    repo.upsert_enrichment(conn, facts.gid, facts.content_hash, _raw(enrichment), en.MODEL, strategy_hash=strategy.text_hash)
```

`_raw` serialises `serves` as a list of dicts; `_enrichment_from_raw` rebuilds `Serve` tuples:

```python
    if isinstance(d.get("serves"), list):
        d["serves"] = tuple(Serve(**s) for s in d["serves"] if isinstance(s, dict))
```

The write-back pair, beside `claim_write_back`:

```python
def claim_serves_write_back(conn, facts: TaskFacts, enrichment: Enrichment, strategy: Strategy) -> dict | None:
    """Spec D5. Returns the payload to write to Asana (serves + role) when
    the claim wins and the judgment is confident; records grooming / none
    outcomes with no Asana write; None when nothing was claimed."""
    if not strategy.goals or facts.serves_estimated is not None:
        return None
    if any(t.casefold().startswith("serves:") for t in facts.tags):
        return None
    conf = enrichment.necessity_confidence
    known = {g.id for g in strategy.goals}
    served = [s for s in enrichment.serves if s.goal in known]
    if served and conf in ("medium", "high"):
        role = max((s.role for s in served), key=lambda r: pz.ROLE_RANK[r])
        payload = {"serves": [s.goal for s in served], "role": role, "confidence": conf}
        return payload if repo.claim_serves(conn, facts.gid, payload) else None
    if not served and conf in ("medium", "high"):
        repo.claim_serves(conn, facts.gid, {"none": True, "confidence": conf})
        return None
    repo.claim_serves(conn, facts.gid, {"grooming": True})
    return None


def write_back_serves(gid: str, payload: dict) -> list[str] | None:
    """After commit: tags, then the comment. A failure logs and keeps the
    claim (D5)."""
    names = [f"serves:{g}" for g in payload["serves"]] + [f"role:{payload['role']}"]
    try:
        gids = tags_service.resolve_gids(names)
        if len(gids) != len(names):
            raise RuntimeError("tag resolution incomplete")
        for tag_gid in gids:
            asana.add_tag(gid, tag_gid)
        asana.create_story(gid, text=en.attach_comment(payload["serves"], payload["role"]))
    except Exception:
        logger.exception("serves write-back failed for gid=%s (claim kept)", gid)
        otel.errors.add(1, {"handler": "prioritize.write_back_serves"})
        return None
    return names
```

`handle_task_changed`: load `strategy = strategy_service.load(stale_after_days=config.strategy_stale_after_days, today=today)` once at the top (where `prioritize_config.load()` is already called — bind `config` to a variable and reuse it). Call `gather(gid, strategy_hash=strategy.text_hash)`. In the per-task loop, after the points claim:

```python
                serves_payload = (
                    claim_serves_write_back(conn, merged, enrichment, strategy) if enrichment else None
                )
                if serves_payload is not None:
                    to_tag.append((facts.gid, serves_payload))
```

(`merged` must carry `serves_estimated` from `previous` the same way it carries `points_estimated`.) After the points write-back block:

```python
    for g, payload in to_tag:
        names = write_back_serves(g, payload)
        if names:
            with get_conn() as conn:
                current = repo.get_facts(conn, g)
                repo.set_tags(conn, g, [*(current.tags if current else ()), *names])
```

`rescore(conn, *, kind, trigger_gid, today, strategy: Strategy | None = None)`:

```python
    strategy = strategy or strategy_service.load(stale_after_days=config.strategy_stale_after_days, today=today)
    states = repo_goals.get_states(conn, today)
    below = frozenset(
        gid for gid, s in states.items()
        if s.kind == "area" and s.state.get("below_the_line") and not s.state.get("muted_until")
    )
    scored = pz.score_set(..., project_last_offered=..., strategy=strategy, below_the_line=below)
```

- [ ] **Step 4: Run the handler suite**

Run: `.venv/bin/pytest tests/test_prioritize_handler.py -q`
Expected: all pass, including the pre-existing write-back tests.

- [ ] **Step 5: Commit**

```bash
git add handlers/prioritize.py tests/test_prioritize_handler.py
git commit -m "feat(strategy): subscriber passes the strategy to enrichment and drafts serves/role tags once"
```

---

### Task 7: Goal state — leads, lag, tripwires, signals with debounce, diagnosis

**Files:**
- Create: `services/goal_state.py`
- Test: `tests/test_goal_state.py`

**Interfaces:**
- Consumes: `models.strategy.*`, `models.prioritize.TaskFacts`, `Config`.
- Produces: `services.goal_state.TaskView(gid, serves: tuple[str, ...], role: str | None, bucket: str, priority: str | None, due_on: date | None, completed: bool, completed_at: datetime | None, tags: tuple[str, ...])`; `views_from(facts: list[TaskFacts], scores: dict[str, dict]) -> list[TaskView]` where `scores[gid] = {"bucket": ..., "components": {...}}`; `evaluate(strategy, views, reports: dict[str, list[dict]], mutes: dict[str, date], previous: dict[str, GoalState], today, config) -> list[GoalState]`; `compare(op, value, threshold) -> bool`; `window_days(period) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_goal_state.py
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from models.strategy import Goal, GoalState, Measure, Signal, Strategy, Tripwire
from services import goal_state as gs
from services import prioritize_config as pc

CFG = pc.load()
TODAY = date(2026, 10, 9)


def view(gid, serves=("finances",), role="path", bucket="next", priority="P1", due_on=None,
         completed=False, completed_days_ago=None, tags=()):
    completed_at = (
        datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc) - timedelta(days=completed_days_ago)
        if completed_days_ago is not None else None
    )
    return gs.TaskView(gid, tuple(serves), role, bucket, priority, due_on, completed or completed_at is not None,
                       completed_at, tuple(tags))


def area(signals, gid="finances"):
    return Goal(id=gid, kind="area", signals=tuple(signals))


def outcome(**kw):
    base = dict(id="consulting", kind="outcome", horizon=date(2027, 3, 31),
                lag=Measure("revenue", ">=", 15000, "month"),
                leads=(Measure("conversation", ">=", 3, "week"),),
                tripwires=(Tripwire(1, "signed-client", "=", 0, date(2026, 12, 31), "Revisit niche"),
                           Tripwire(2, "lag", "<", 5000, date(2027, 1, 31), "Revisit pricing")))
    return Goal(**(base | kw))


def enough(n=5, **kw):
    return [view(f"f{i}", **kw) for i in range(n)]


def run(goals, views, reports=None, mutes=None, previous=None, today=TODAY, config=CFG):
    strat = Strategy(goals=tuple(goals), text_hash="h")
    return {s.goal_id: s for s in gs.evaluate(strat, views, reports or {}, mutes or {}, previous or {}, today, config)}


OVERDUE = Signal(kind="overdue", cls="evidence", text="overdue")


def test_overdue_default_requires_p1_path_actionable_past_grace():
    late = TODAY - timedelta(days=5)
    cases = {
        "counts": view("a", due_on=late),
        "support_role": view("b", role="support", due_on=late),
        "p2": view("c", priority="P2", due_on=late),
        "snoozed": view("d", bucket="snoozed", due_on=late),
        "waiting": view("e", bucket="nudge", due_on=late),
        "inside_grace": view("f", due_on=TODAY - timedelta(days=2)),
        "no_date": view("g"),
    }
    for name, v in cases.items():
        s = run([area([OVERDUE])], enough() + [v])["finances"]
        sig = s.state["signals"][0]
        assert sig["raw"] is (name == "counts"), name
        assert sig["tasks"] == (["a"] if name == "counts" else []), name


def test_overdue_widened_by_priority_and_grace():
    wide = Signal(kind="overdue", cls="evidence", text="overdue:P2+ grace 0", min_priority="P2", grace=0)
    s = run([area([wide])], enough() + [view("c", priority="P2", due_on=TODAY - timedelta(days=1))])
    assert s["finances"].state["signals"][0]["raw"] is True


def test_overdue_tag_variant_ignores_priority():
    tagged = Signal(kind="overdue", cls="evidence", text="overdue:bill", min_priority="P3", tag="bill")
    s = run([area([tagged])], enough() + [view("c", priority="P3", due_on=TODAY - timedelta(days=9), tags=("bill",))])
    assert s["finances"].state["signals"][0]["raw"] is True


def test_undated_after_date():
    sig = Signal(kind="undated", cls="evidence", text="undated:tax after 2026-10-01", tag="tax", after=date(2026, 10, 1))
    s = run([area([sig])], enough() + [view("t", tags=("tax",))])
    assert s["finances"].state["signals"][0]["raw"] is True
    s = run([area([sig])], enough() + [view("t", tags=("tax",))], today=date(2026, 9, 30))
    assert s["finances"].state["signals"][0]["raw"] is False


def test_stale_is_no_data_without_open_actionable_tasks():
    sig = Signal(kind="stale", cls="absence", text="stale > 14 days", days=14)
    done = [view(f"d{i}", completed_days_ago=30) for i in range(5)]
    assert run([area([sig])], done)["finances"].state["signals"][0]["raw"] is None
    assert run([area([sig])], done + [view("open")])["finances"].state["signals"][0]["raw"] is True
    recent = done + [view("open"), view("r", completed_days_ago=3)]
    assert run([area([sig])], recent)["finances"].state["signals"][0]["raw"] is False


def test_lead_signal_is_no_data_without_tag_history():
    sig = Signal(kind="lead", cls="absence", text="lead bill < 2 per month", tag="bill", value=2, period="month")
    assert run([area([sig])], enough())["finances"].state["signals"][0]["raw"] is None
    old = enough() + [view("x", completed_days_ago=80, tags=("bill",))]
    assert run([area([sig])], old)["finances"].state["signals"][0]["raw"] is True
    fresh = old + [view("y", completed_days_ago=2, tags=("bill",)), view("z", completed_days_ago=5, tags=("bill",))]
    assert run([area([sig])], fresh)["finances"].state["signals"][0]["raw"] is False


def test_rollout_guard_reports_no_data_under_min_tagged():
    s = run([area([OVERDUE])], [view("a", due_on=TODAY - timedelta(days=9))])
    assert s["finances"].state["signals"][0]["raw"] is None and s["finances"].state["below_the_line"] is False


def _prev(raw_days, effective=False):
    return {"finances": GoalState("finances", "area", TODAY - timedelta(days=1), "h", {
        "signals": [{"signal": "overdue", "raw": True, "effective": effective, "consecutive_days": raw_days}]})}


def test_debounce_flips_after_three_consecutive_days_and_clears_after_three():
    late = enough() + [view("a", due_on=TODAY - timedelta(days=9))]
    assert run([area([OVERDUE])], late)["finances"].state["signals"][0]["effective"] is False
    s = run([area([OVERDUE])], late, previous=_prev(2))["finances"].state["signals"][0]
    assert s["effective"] is True and s["consecutive_days"] == 3
    # now clear: raw false today, effective was true
    prev_true = {"finances": GoalState("finances", "area", TODAY - timedelta(days=1), "h", {
        "signals": [{"signal": "overdue", "raw": False, "effective": True, "consecutive_days": 2}]})}
    s = run([area([OVERDUE])], enough(), previous=prev_true)["finances"].state["signals"][0]
    assert s["effective"] is False and s["consecutive_days"] == 3
    prev_true["finances"].state["signals"][0]["consecutive_days"] = 1
    s = run([area([OVERDUE])], enough(), previous=prev_true)["finances"].state["signals"][0]
    assert s["effective"] is True and s["consecutive_days"] == 2


def test_mute_hides_below_the_line_but_keeps_raw():
    late = enough() + [view("a", due_on=TODAY - timedelta(days=9))]
    s = run([area([OVERDUE])], late, mutes={"finances": TODAY + timedelta(days=3)}, previous=_prev(2))["finances"]
    assert s.state["signals"][0]["raw"] is True and s.state["signals"][0]["effective"] is True
    assert s.state["below_the_line"] is False and s.state["muted_until"] == (TODAY + timedelta(days=3)).isoformat()


def test_lead_rates_and_lag():
    views = [view(f"c{i}", serves=("consulting",), completed_days_ago=i, tags=("conversation",)) for i in (1, 2, 3, 9)]
    reports = {"consulting": [{"value": 4000.0, "period_start": date(2026, 9, 1), "reported_at": None}]}
    s = run([outcome()], views, reports=reports)["consulting"].state
    lead = s["leads"][0]
    assert (lead["tag"], lead["window"], lead["value"], lead["threshold"], lead["met"]) == ("conversation", "week", 3, 3.0, True)
    assert s["lag"] == {"value": 4000.0, "threshold": 15000.0, "met": False, "period_start": "2026-09-01"}


def test_tripwire_fires_only_from_its_by_date():
    g = outcome()
    s = run([g], [], today=date(2026, 12, 30))["consulting"].state["tripwires"]
    assert s[0]["fired"] is False and s[0]["evaluated"] is False
    s = run([g], [], today=date(2026, 12, 31))["consulting"].state["tripwires"]
    assert s[0]["fired"] is True and s[0]["value"] == 0
    signed = [view("w", serves=("consulting",), completed_days_ago=10, tags=("signed-client",))]
    s = run([g], signed, today=date(2026, 12, 31))["consulting"].state["tripwires"]
    assert s[0]["fired"] is False


def test_lag_tripwire_uses_latest_report_and_cannot_fire_without_one():
    g = outcome()
    s = run([g], [], today=date(2027, 2, 1))["consulting"].state["tripwires"][1]
    assert s["fired"] is False and s["value"] is None
    s = run([g], [], reports={"consulting": [{"value": 1000.0, "period_start": None, "reported_at": None}]},
            today=date(2027, 2, 1))["consulting"].state["tripwires"][1]
    assert s["fired"] is True


def test_diagnosis_rule():
    strong = [view(f"c{i}", serves=("consulting",), completed_days_ago=i, tags=("conversation",)) for i in (1, 2, 3)]
    flat = {"consulting": [{"value": 100.0, "period_start": None, "reported_at": None}] * 2}
    assert run([outcome()], strong, reports=flat)["consulting"].state["diagnosis"] == "lead strong, lag flat"
    one = {"consulting": [{"value": 100.0, "period_start": None, "reported_at": None}]}
    assert run([outcome()], strong, reports=one)["consulting"].state["diagnosis"] == "insufficient data"
    assert run([outcome()], [], reports=flat)["consulting"].state["diagnosis"] == "lead weak"
    met = {"consulting": [{"value": 20000.0, "period_start": None, "reported_at": None}] * 2}
    assert run([outcome()], strong, reports=met)["consulting"].state["diagnosis"] == "on track"


def test_views_from_reads_components_and_facts():
    from tests.test_prioritize import facts
    f = facts("a", due_on=TODAY, tags=("bill",))
    scores = {"a": {"bucket": "next", "components": {"serves": ["finances"], "role": "derisk"}}}
    v = gs.views_from([f], scores)[0]
    assert (v.serves, v.role, v.bucket, v.priority, v.due_on, v.tags) == (("finances",), "derisk", "next", "P1", TODAY, ("bill",))
    assert gs.views_from([f], {})[0].bucket == "next"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_goal_state.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.goal_state'`

- [ ] **Step 3: Implement**

```python
# services/goal_state.py
"""Daily evaluation of each goal and area: lead rates, the latest lag,
tripwires, below-the-line signals with debounce, and the lead/lag
diagnosis. Pure — the handler supplies views built from task_facts and
yesterday's task_scores. Design: strategy-layer spec D7, D9, D11."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from models.prioritize import TaskFacts
from models.strategy import PERIOD_DAYS, Goal, GoalState, Signal, Strategy
from services.prioritize_config import Config

PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
LEAD_HISTORY_DAYS = 90
ACTIONABLE = "next"


@dataclass(frozen=True)
class TaskView:
    gid: str
    serves: tuple[str, ...]
    role: str | None
    bucket: str
    priority: str | None
    due_on: date | None
    completed: bool
    completed_at: datetime | None
    tags: tuple[str, ...]


def views_from(facts: list[TaskFacts], scores: dict[str, dict]) -> list[TaskView]:
    out = []
    for f in facts:
        c = (scores.get(f.gid) or {}).get("components") or {}
        bucket = (scores.get(f.gid) or {}).get("bucket") or ("excluded:completed" if f.completed else ACTIONABLE)
        serves = tuple(c.get("serves") or ())
        if not serves:  # a completed task has no fresh score row; fall back to its tags
            serves = tuple(t.partition(":")[2] for t in f.tags if t.casefold().startswith("serves:"))
        out.append(TaskView(f.gid, serves, c.get("role"), bucket, f.priority, f.due_on, f.completed, f.completed_at, f.tags))
    return out


def compare(op: str, value: float, threshold: float) -> bool:
    return {">=": value >= threshold, "<=": value <= threshold, "=": value == threshold,
            "<": value < threshold, ">": value > threshold}[op]


def window_days(period: str) -> int:
    return PERIOD_DAYS[period]


def _completed_within(views: list[TaskView], goal_id: str, tag: str | None, days: int, today: date) -> list[TaskView]:
    start = today - timedelta(days=days)
    return [
        v for v in views
        if v.completed and v.completed_at and v.completed_at.date() > start
        and goal_id in v.serves and (tag is None or tag in v.tags)
    ]


def _count_since(views: list[TaskView], goal_id: str, tag: str, since: date | None, today: date) -> int:
    days = (today - since).days if since else 10_000
    return len(_completed_within(views, goal_id, tag, days, today))


def _signal(sig: Signal, goal: Goal, views: list[TaskView], today: date, tagged: int, config: Config) -> tuple[bool | None, list[str]]:
    if tagged < config.min_tagged_for_signals:
        return None, []
    mine = [v for v in views if goal.id in v.serves]
    open_actionable = [v for v in mine if not v.completed and v.bucket == ACTIONABLE]
    if sig.kind == "overdue":
        hits = [
            v for v in open_actionable
            if v.role in ("path", "derisk")
            and v.due_on is not None and (today - v.due_on).days > sig.grace
            and (sig.tag in v.tags if sig.tag else PRIORITY_RANK.get(v.priority or "P2", 2) <= PRIORITY_RANK[sig.min_priority])
        ]
        return bool(hits), [v.gid for v in hits]
    if sig.kind == "undated":
        if sig.after is None or today <= sig.after:
            return False, []
        hits = [v for v in open_actionable if sig.tag in v.tags and v.due_on is None]
        return bool(hits), [v.gid for v in hits]
    if sig.kind == "stale":
        if not open_actionable:
            return None, []
        recent = _completed_within(views, goal.id, None, sig.days or 0, today)
        return not recent, []
    if sig.kind == "lead":
        history = _completed_within(views, goal.id, sig.tag, LEAD_HISTORY_DAYS, today)
        if not history:
            return None, []
        count = len(_completed_within(views, goal.id, sig.tag, window_days(sig.period or "week"), today))
        return count < (sig.value or 0), []
    return None, []


def _debounce(prev: dict | None, raw: bool | None, config: Config) -> tuple[bool, int]:
    """(effective, consecutive_days). A run counts days the raw value has
    held its current truth; effective flips only when the run reaches
    signal_debounce_days (D9)."""
    if raw is None:
        return False, 0
    p_raw = prev.get("raw") if prev else None
    p_eff = bool(prev.get("effective")) if prev else False
    p_run = int(prev.get("consecutive_days") or 0) if prev else 0
    run = p_run + 1 if p_raw is raw else 1
    if raw and not p_eff:
        return run >= config.signal_debounce_days, run
    if not raw and p_eff:
        return not (run >= config.signal_debounce_days), run
    return p_eff, run


def _area_state(goal: Goal, views: list[TaskView], mutes: dict[str, date], prev: GoalState | None, today: date, config: Config) -> dict:
    tagged = sum(1 for v in views if goal.id in v.serves)
    prev_signals = {s["signal"]: s for s in (prev.state.get("signals") if prev else []) or []}
    signals = []
    for sig in goal.signals:
        raw, tasks = _signal(sig, goal, views, today, tagged, config)
        effective, run = _debounce(prev_signals.get(sig.text), raw, config)
        signals.append({"signal": sig.text, "class": sig.cls, "raw": raw, "effective": effective,
                        "consecutive_days": run, "state": "no_data" if raw is None else ("true" if effective else "false"),
                        "tasks": tasks})
    muted_until = mutes.get(goal.id)
    muted = muted_until is not None and muted_until >= today
    return {
        "signals": signals,
        "below_the_line": (not muted) and any(s["effective"] for s in signals),
        "evidence_below_the_line": (not muted) and any(s["effective"] and s["class"] == "evidence" for s in signals),
        "muted_until": muted_until.isoformat() if muted else None,
        "tagged": tagged,
    }


def _outcome_state(goal: Goal, views: list[TaskView], reports: list[dict], last_reviewed: date | None, today: date, config: Config) -> dict:
    leads = []
    for m in goal.leads:
        count = len(_completed_within(views, goal.id, m.tag, window_days(m.period), today))
        leads.append({"tag": m.tag, "window": m.period, "value": count, "threshold": m.value, "met": compare(m.op, count, m.value)})
    lag = None
    if goal.lag and reports:
        latest = reports[0]
        ps = latest.get("period_start")
        lag = {"value": float(latest["value"]), "threshold": goal.lag.value,
               "met": compare(goal.lag.op, float(latest["value"]), goal.lag.value),
               "period_start": ps.isoformat() if isinstance(ps, date) else ps}
    tripwires = []
    for t in goal.tripwires:
        evaluated = today >= t.by
        if t.subject == "lag":
            value = float(reports[0]["value"]) if reports else None
        else:
            value = _count_since(views, goal.id, t.subject, last_reviewed, today)
        fired = bool(evaluated and value is not None and compare(t.op, value, t.value))
        tripwires.append({"ordinal": t.ordinal, "text": f"{t.subject} {t.op} {t.value:g} by {t.by.isoformat()}",
                          "action": t.action, "by": t.by.isoformat(), "value": value, "evaluated": evaluated, "fired": fired})
    leads_met = bool(leads) and all(x["met"] for x in leads)
    if goal.lag is None or len(reports) < config.lag_flat_periods:
        diagnosis = "insufficient data"
    elif not leads_met:
        diagnosis = "lead weak"
    elif all(not compare(goal.lag.op, float(r["value"]), goal.lag.value) for r in reports[: config.lag_flat_periods]):
        diagnosis = "lead strong, lag flat"
    else:
        diagnosis = "on track"
    return {"leads": leads, "lag": lag, "tripwires": tripwires, "diagnosis": diagnosis,
            "horizon": goal.horizon.isoformat() if goal.horizon else None}


def evaluate(strategy: Strategy, views: list[TaskView], reports: dict[str, list[dict]], mutes: dict[str, date],
             previous: dict[str, GoalState], today: date, config: Config) -> list[GoalState]:
    out = []
    for g in strategy.goals:
        if g.kind == "area":
            state = _area_state(g, views, mutes, previous.get(g.id), today, config)
        else:
            state = _outcome_state(g, views, reports.get(g.id, []), strategy.last_reviewed, today, config)
        state.setdefault("next_step", None)
        state.setdefault("stalled", True)
        out.append(GoalState(g.id, g.kind, today, strategy.text_hash, state))
    return out
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_goal_state.py -q`
Expected: all pass. If `test_debounce_*` fails on the clear case, trace `_debounce`: with `p_raw=False, p_eff=True, p_run=2` and `raw=False`, `run` must be 3 and `effective` must be `False`.

- [ ] **Step 5: Commit**

```bash
git add services/goal_state.py tests/test_goal_state.py
git commit -m "feat(strategy): daily goal-state evaluation with debounced signals and lead/lag diagnosis"
```

---

### Task 8: Daily tick — evaluate, fire tripwires as tasks, rescore, record next steps

**Files:**
- Modify: `handlers/prioritize.py`
- Modify: `clients/otel.py` (two counters, two gauges)
- Test: `tests/test_prioritize_handler.py`

**Interfaces:**
- Consumes: `services.goal_state.evaluate`, `views_from`; `repo.goals.*`; `repo.prioritize.list_scores`; `asana.find_task_by_external`, `asana.create_task_from_fields`, `asana.add_task_to_section`, `sections.for_category("review")`, `asana.ASANA_PROJECT_ID`, `tags_service.resolve_gids`, `pubsub.publish_task_changed`.
- Produces: `handlers.prioritize.evaluate_goals(conn, strategy, today) -> list[GoalState]` (always saves the strategy snapshot first); `fire_tripwires(strategy, states, previous) -> list[str]` (external ids created); `TRIPWIRE_EXTERNAL = "tripwire:{goal}:{ordinal}:{by}"`; `handle_day_changed` returns `{"deferred", "started", "healed", "goals", "tripwires_fired"}`; `otel.tripwire_fired` (counter), `otel.strategy_goals_loaded`, `otel.area_below_the_line` (gauges), `otel.necessity_judgments` (counter).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_prioritize_handler.py
from models.strategy import GoalState, Measure, Tripwire


def _fire_strat():
    return Strategy(goals=(Goal(id="consulting", kind="outcome", horizon=date(2027, 3, 31),
                                leads=(Measure("conversation", ">=", 3, "week"),),
                                tripwires=(Tripwire(1, "signed-client", "=", 0, date(2026, 9, 1), "Revisit niche"),)),),
                    text_hash="sh")


@pytest.fixture
def goals_db(monkeypatch, db):
    db.goal_states, db.reports, db.mutes, db.next_steps = {}, {}, {}, []
    monkeypatch.setattr(repo_goals, "upsert_state", lambda c, s: c.goal_states.__setitem__((s.goal_id, s.day), s))
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {k[0]: v for k, v in c.goal_states.items() if k[1] == day})
    monkeypatch.setattr(repo_goals, "all_latest_reports", lambda c, limit=3: dict(c.reports))
    monkeypatch.setattr(repo_goals, "get_mutes", lambda c: dict(c.mutes))
    monkeypatch.setattr(repo_goals, "set_next_steps", lambda c, day, steps: c.next_steps.append((day, steps)))
    monkeypatch.setattr(repo_goals, "save_snapshot", lambda c, strat: c.__dict__.__setitem__("snapshot", strat))
    monkeypatch.setattr(repo, "list_scores", lambda c: [])
    monkeypatch.setattr(h, "heal", lambda: 0)
    monkeypatch.setattr(h, "settle_deferrals", lambda c, today: (0, 0))
    return db


@pytest.fixture
def tripwire_asana(monkeypatch):
    created, existing = [], {}
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: existing.get(ext))
    monkeypatch.setattr(asana, "create_task_from_fields", lambda fields: (created.append(fields), type("T", (), {"gid": f"new{len(created)}", "permalink_url": "u"})())[1])
    monkeypatch.setattr(asana, "add_task_to_section", lambda gid, sec: None)
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj")
    monkeypatch.setattr(h.tags_service, "resolve_gids", lambda names: [f"g-{n}" for n in names])
    monkeypatch.setattr(ps, "publish_task_changed", lambda gid, source: None)
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    return created, existing


def test_day_changed_evaluates_fires_and_records_next_step(goals_db, tripwire_asana, monkeypatch):
    created, _ = tripwire_asana
    monkeypatch.setattr(st, "load", lambda **kw: _fire_strat())
    out = h.handle_day_changed(today=TODAY)
    assert out["goals"] == 1 and out["tripwires_fired"] == 1
    assert created[0]["name"] == "[P1] Revisit niche"
    assert created[0]["external"] == {"gid": "tripwire:consulting:1:2026-09-01", "data": "tasks"}
    assert created[0]["tags"] == ["g-serves:consulting", "g-role:derisk", "g-tripwire"]
    assert ("consulting", TODAY) in goals_db.goal_states
    assert goals_db.next_steps == [(TODAY, {"consulting": None})]


def test_tripwire_already_fired_yesterday_is_not_recreated(goals_db, tripwire_asana, monkeypatch):
    created, _ = tripwire_asana
    monkeypatch.setattr(st, "load", lambda **kw: _fire_strat())
    yesterday = TODAY - timedelta(days=1)
    goals_db.goal_states[("consulting", yesterday)] = GoalState("consulting", "outcome", yesterday, "sh",
        {"tripwires": [{"ordinal": 1, "by": "2026-09-01", "fired": True}]})
    assert h.handle_day_changed(today=TODAY)["tripwires_fired"] == 0 and created == []


def test_tripwire_external_id_dedupes_against_asana(goals_db, tripwire_asana, monkeypatch):
    created, existing = tripwire_asana
    existing["tripwire:consulting:1:2026-09-01"] = "old"
    monkeypatch.setattr(st, "load", lambda **kw: _fire_strat())
    assert h.handle_day_changed(today=TODAY)["tripwires_fired"] == 0 and created == []


def test_edited_by_date_is_a_new_tripwire(goals_db, tripwire_asana, monkeypatch):
    created, existing = tripwire_asana
    existing["tripwire:consulting:1:2026-09-01"] = "old"
    strat = _fire_strat()
    g = strat.goals[0]
    moved = Strategy(goals=(replace_facts(g, tripwires=(replace_facts(g.tripwires[0], by=date(2026, 10, 1)),)),), text_hash="sh2")
    monkeypatch.setattr(st, "load", lambda **kw: moved)
    assert h.handle_day_changed(today=TODAY)["tripwires_fired"] == 1
    assert created[0]["external"]["gid"] == "tripwire:consulting:1:2026-10-01"


def test_tripwire_creation_failure_raises_for_redelivery(goals_db, tripwire_asana, monkeypatch):
    monkeypatch.setattr(st, "load", lambda **kw: _fire_strat())

    def boom(fields):
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "create_task_from_fields", boom)
    with pytest.raises(RuntimeError):
        h.handle_day_changed(today=TODAY)


def test_no_strategy_day_changed_is_unchanged(goals_db, monkeypatch):
    monkeypatch.setattr(st, "load", lambda **kw: Strategy.EMPTY)
    out = h.handle_day_changed(today=TODAY)
    assert out["goals"] == 0 and out["tripwires_fired"] == 0 and goals_db.goal_states == {}
    assert goals_db.snapshot == Strategy.EMPTY  # an emptied document clears the API's view too
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_prioritize_handler.py -q -k "day_changed or tripwire"`
Expected: FAIL — `KeyError: 'goals'` on the returned dict.

- [ ] **Step 3: Implement**

In `clients/otel.py` add module-level no-op defaults and registrations (same shape as `prioritize_candidates`):

```python
tripwire_fired: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
necessity_judgments: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
strategy_goals_loaded: metrics._Gauge = metrics.NoOpMeter("noop").create_gauge("noop")
area_below_the_line: metrics._Gauge = metrics.NoOpMeter("noop").create_gauge("noop")
```

with `global` declarations in `setup_telemetry` and `meter.create_counter("asana.strategy.tripwire_fired", ...)`, `meter.create_counter("asana.strategy.necessity_judgments", description="by confidence and outcome (attached|grooming|none)")`, `meter.create_gauge("asana.strategy.goals_loaded")`, `meter.create_gauge("asana.strategy.area_below_the_line")`.

In `handlers/prioritize.py`:

```python
import clients.pubsub as pubsub
from services import goal_state as gs
from services import sections

TRIPWIRE_EXTERNAL = "tripwire:{goal}:{ordinal}:{by}"


def evaluate_goals(conn, strategy: Strategy, today: date) -> list[GoalState]:
    """Spec D7: views from facts plus yesterday's score rows (bucket, serves,
    role); previous day's states for the debounce; writes today's rows."""
    repo_goals.save_snapshot(conn, strategy)  # the API and webhook CF read goals from here, never the secret
    if not strategy.goals:
        return []
    scores = {r["task_gid"]: {"bucket": r["bucket"], "components": r["components"]} for r in repo.list_scores(conn)}
    views = gs.views_from(repo.list_facts(conn), scores)
    config = prioritize_config.load()
    states = gs.evaluate(
        strategy, views, repo_goals.all_latest_reports(conn), repo_goals.get_mutes(conn),
        repo_goals.get_states(conn, today - timedelta(days=1)), today, config,
    )
    for s in states:
        repo_goals.upsert_state(conn, s)
        if s.kind == "area":
            otel.area_below_the_line.set(1 if s.state.get("below_the_line") else 0, {"area": s.goal_id})
    otel.strategy_goals_loaded.set(len(strategy.outcome_goals()), {"kind": "outcome"})
    otel.strategy_goals_loaded.set(len(strategy.areas()), {"kind": "area"})
    return states


def fire_tripwires(strategy: Strategy, states: list[GoalState], previous: dict[str, GoalState]) -> list[str]:
    """Spec D8: a tripwire newly fired today becomes one task, guarded by an
    external id that includes the `by` date. Asana failure raises (D7)."""
    fired: list[str] = []
    for s in states:
        goal = strategy.get(s.goal_id)
        if goal is None or goal.kind != "outcome":
            continue
        prev = {(t["ordinal"], t["by"]): t for t in (previous.get(s.goal_id).state.get("tripwires") if previous.get(s.goal_id) else []) or []}
        for t in s.state.get("tripwires") or []:
            if not t["fired"] or prev.get((t["ordinal"], t["by"]), {}).get("fired"):
                continue
            external = TRIPWIRE_EXTERNAL.format(goal=goal.id, ordinal=t["ordinal"], by=t["by"])
            if asana.find_task_by_external(external):
                continue
            fields = {
                "name": f"[P1] {t['action']}",
                "html_notes": (
                    f"<body>Tripwire fired for <b>{goal.id}</b>: {t['text']} (measured {t['value']}).\n"
                    f"Review: task-next review</body>"
                ),
                "projects": [asana.ASANA_PROJECT_ID],
                "external": {"gid": external, "data": "tasks"},
                "tags": tags_service.resolve_gids([f"serves:{goal.id}", "role:derisk", "tripwire"]),
            }
            created = asana.create_task_from_fields(fields)
            section = sections.for_category("review")
            if section:
                asana.add_task_to_section(created.gid, section)
            pubsub.publish_task_changed(created.gid, "pipeline")
            otel.tripwire_fired.add(1, {"goal": goal.id})
            fired.append(external)
    return fired
```

`handle_day_changed` becomes:

```python
def handle_day_changed(*, today: date | None = None) -> dict:
    today = today or today_local()
    config = prioritize_config.load()
    strategy = strategy_service.load(stale_after_days=config.strategy_stale_after_days, today=today)
    with get_conn() as conn:
        deferred, started = settle_deferrals(conn, today)
    healed = heal()
    with get_conn() as conn:
        previous = repo_goals.get_states(conn, today - timedelta(days=1))
        states = evaluate_goals(conn, strategy, today)
    fired = fire_tripwires(strategy, states, previous)
    with get_conn() as conn:
        scored = rescore(conn, kind="daily", trigger_gid=None, today=today, strategy=strategy)
        if states:
            steps = {}
            for g in strategy.outcome_goals():
                step = next((t for t in scored.next() if t.components.get("role") == "path" and g.id in (t.components.get("serves") or [])), None)
                steps[g.id] = step.gid if step else None
            repo_goals.set_next_steps(conn, today, steps)
    logger.info("day_changed %s: %d deferred, %d started, %d republished, %d goals, %d tripwires",
                today, deferred, started, healed, len(states), len(fired))
    return {"deferred": deferred, "started": started, "healed": healed, "goals": len(states), "tripwires_fired": len(fired)}
```

(`scored.next()` is already position-ordered, so the first match is the highest-ranked path task.)

- [ ] **Step 4: Run the handler suite**

Run: `.venv/bin/pytest tests/test_prioritize_handler.py tests/test_otel.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add handlers/prioritize.py clients/otel.py tests/test_prioritize_handler.py
git commit -m "feat(strategy): daily tick evaluates goals, fires tripwires as tasks, records next steps"
```

---

### Task 9: Gate 2 reads the strategy and judges necessity

**Files:**
- Modify: `models/events.py` (`Decision`)
- Modify: `services/triage.py`
- Modify: `clients/claude.py:127-170` (`run_agent`)
- Test: `tests/test_triage.py`, `tests/test_claude_agent.py`

**Interfaces:**
- Consumes: `services.strategy.load`, `services.strategy.section_text`, `models.strategy.ROLES`.
- Produces: `Decision.serves: list[dict]` (each `{goal, role, confidence}`, unknown goals dropped), `Decision.necessity_confidence: str = "low"`, `Decision.necessity_reason: str = ""`; `triage.system_blocks(strategy_text) -> list[dict]`; `claude.run_agent(system: str | list[dict], ...)`.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_triage.py
from services import strategy as st_service
from models.strategy import Goal, Strategy

STRAT = Strategy(goals=(Goal(id="consulting", kind="outcome"),), text_hash="h")


def _strategy(monkeypatch, loaded=STRAT, text="### consulting\n- kind: outcome\n"):
    monkeypatch.setattr(st_service, "load", lambda **kw: loaded)
    monkeypatch.setattr(st_service, "section_text", lambda: text)


def _ok_serves(**kw):
    base = json.loads(_ok(actionable=True))
    base.update({"serves": [{"goal": "consulting", "role": "path", "confidence": "high"}],
                 "necessity_confidence": "high", "necessity_reason": "next step"})
    base.update(kw)
    return json.dumps(base)


def test_strategy_is_a_second_cached_system_block(monkeypatch):
    _roles(monkeypatch, "### R\nfact")
    _strategy(monkeypatch)
    captured = {}
    _agent(monkeypatch, _ok_serves(), capture=captured)
    _gid_verifies(monkeypatch, True)
    triage.decide(make_email_event(), today="2026-10-09")
    system = captured["system"]
    assert isinstance(system, list) and system[0]["text"] == triage.SYSTEM_PROMPT
    assert system[1]["text"].startswith("## Strategy") and system[1]["cache_control"] == {"type": "ephemeral"}


def test_serves_is_parsed_and_unknown_goals_dropped(monkeypatch):
    _roles(monkeypatch, "")
    _strategy(monkeypatch)
    _agent(monkeypatch, _ok_serves(serves=[{"goal": "consulting", "role": "path", "confidence": "high"},
                                           {"goal": "ghost", "role": "support", "confidence": "low"}]))
    _gid_verifies(monkeypatch, True)
    d = triage.decide(make_email_event(), today="2026-10-09")
    assert d.actionable and d.serves == [{"goal": "consulting", "role": "path", "confidence": "high"}]
    assert d.necessity_confidence == "high" and d.necessity_reason == "next step"


def test_no_strategy_means_one_system_block_and_defaults(monkeypatch):
    _roles(monkeypatch, "")
    _strategy(monkeypatch, loaded=Strategy.EMPTY, text="")
    captured = {}
    _agent(monkeypatch, _ok(actionable=True), capture=captured)
    d = triage.decide(make_email_event(), today="2026-10-09")
    assert len(captured["system"]) == 1
    assert d.serves == [] and d.necessity_confidence == "low"


def test_schema_requires_the_three_fields():
    assert {"serves", "necessity_confidence", "necessity_reason"} <= set(triage.OUTPUT_SCHEMA["required"])
```

```python
# append to tests/test_claude_agent.py
def test_run_agent_accepts_system_blocks(monkeypatch):
    captured = {}

    class Runner:
        def __iter__(self):
            msg = type("M", (), {"stop_reason": "end_turn", "content": [type("B", (), {"type": "text", "text": "{}"})()],
                                 "usage": type("U", (), {"input_tokens": 1, "output_tokens": 1})()})()
            return iter([msg])

    class Messages:
        @staticmethod
        def tool_runner(**kw):
            captured.update(kw)
            return Runner()

    class Beta:
        messages = Messages()

    class Client:
        beta = Beta()

        def with_options(self, **kw):
            return self

    monkeypatch.setattr(claude, "_get_client", lambda: Client())
    blocks = [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "b"}]
    text, stop = claude.run_agent(system=blocks, user="u", tools=[], output_schema={"type": "object"})
    assert captured["system"] == blocks and stop == "end_turn"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_triage.py tests/test_claude_agent.py -q`
Expected: FAIL — `captured["system"]` is a string; `Decision` has no `serves`.

- [ ] **Step 3: Implement**

`models/events.py`, `Decision` gains (after `outcome`):

```python
    serves: list = field(default_factory=list)  # [{goal, role, confidence}], unknown goals dropped (D14)
    necessity_confidence: str = "low"
    necessity_reason: str = ""
```

`clients/claude.py::run_agent` — change `system: str` to `system: str | list[dict]` and build the blocks exactly as in `extract_structured` (Task 3, Step 5), passing `system=blocks` to `tool_runner`.

`services/triage.py`:

```python
from models.strategy import ROLES
from services import standing_context, strategy as strategy_service
```

`OUTPUT_SCHEMA["properties"]` gains the same `serves`, `necessity_confidence`, `necessity_reason` entries as enrichment (Task 3, Step 4), and `required` gains the three names. Append to `SYSTEM_PROMPT`, before "Respond with the JSON object only.":

```
- If a ## Strategy section is present in your instructions, also judge what a task from this email would serve: serves is a list of {goal, role, confidence} where goal is a ### id from the strategy and role is path (a precondition on an outcome goal's written path, or its obvious next step), derisk (its absence puts the outcome or an area's standard at significant risk) or support (helps, but not necessary). An empty serves with high confidence means the email serves no goal or area and is a real answer. necessity_confidence is your confidence in the serves list as a whole; necessity_reason is one sentence. Strategy never makes a non-actionable email actionable. Without a ## Strategy section, return an empty serves list with low confidence.
```

Add:

```python
def system_blocks(strategy_text: str) -> list[dict]:
    blocks = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    if strategy_text.strip():
        blocks.append({"type": "text", "text": "## Strategy\n\n" + strategy_text.strip(),
                       "cache_control": {"type": "ephemeral"}})
    return blocks
```

`_parse(text, stop, message_id, *, gid_exists=_gid_exists, known_goals: tuple[str, ...] = ())` reads the three fields:

```python
    raw_serves = data.get("serves")
    serves = [
        {"goal": s["goal"], "role": s["role"], "confidence": s["confidence"]}
        for s in (raw_serves if isinstance(raw_serves, list) else [])
        if isinstance(s, dict) and s.get("goal") in known_goals and s.get("role") in ROLES
        and s.get("confidence") in ("low", "medium", "high")
    ]
    necessity = {
        "serves": serves,
        "necessity_confidence": data.get("necessity_confidence") if data.get("necessity_confidence") in ("low", "medium", "high") else "low",
        "necessity_reason": str(data.get("necessity_reason") or "").strip(),
    }
```

and passes `**necessity` into every non-fail-open `Decision(...)` it constructs. `decide` loads the strategy once:

```python
    strategy = strategy_service.load()
    known = tuple(g.id for g in strategy.goals)
    text, stop = claude.run_agent(
        system=system_blocks(strategy_service.section_text() if strategy.goals else ""),
        ...
    )
    decision = _parse(text, stop, message_id, known_goals=known)
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_triage.py tests/test_triage_tools.py tests/test_claude_agent.py -q`
Expected: all pass. Pre-existing triage tests that construct `Decision(...)` for equality still pass because the new fields default.

- [ ] **Step 5: Commit**

```bash
git add models/events.py services/triage.py clients/claude.py tests/test_triage.py tests/test_claude_agent.py
git commit -m "feat(strategy): gate 2 reads the strategy block and returns serves/necessity"
```

---

### Task 10: Creation path — tags at birth, necessity suppression, restore seam

**Files:**
- Modify: `handlers/task_create.py`
- Test: `tests/test_task_create.py`

**Interfaces:**
- Consumes: `Decision.serves/necessity_confidence/necessity_reason`; `prioritize_config.load().necessity_mode`; `repo_tasks.insert(serves_estimated=)`; `repo_suppressions.insert(web_link=)`; `strategy_service.load`.
- Produces: `task_create.necessity_outcome(decision, mode, has_strategy) -> tuple[str, list[str]]` returning `("tag"|"create"|"suppress", tag_names)`; `task_create.create_from_event(event, verdict, *, extra_tags: list[str] = (), serves_estimated: dict | None = None) -> CreatedTask | None` (everything after the gates, used by restore in Task 11); `_suppress(..., web_link=)` stored.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_task_create.py
from handlers import task_create as tc
from models.strategy import Goal, Strategy
from services import prioritize_config as pc
from services import strategy as st_service

HIGH = dict(serves=[{"goal": "consulting", "role": "path", "confidence": "high"}], necessity_confidence="high",
            necessity_reason="next step")


def _mode(monkeypatch, mode):
    cfg = pc.load()
    monkeypatch.setattr(pc, "load", lambda path=None: replace(cfg, necessity_mode=mode))


def _strategy_on(monkeypatch, on=True):
    monkeypatch.setattr(st_service, "load", lambda **kw: Strategy(goals=(Goal(id="consulting", kind="outcome"),)) if on else Strategy.EMPTY)


def test_necessity_outcome_table():
    d = lambda **kw: Decision(**kw)  # noqa: E731
    assert tc.necessity_outcome(d(**HIGH), "flag", True) == ("tag", ["serves:consulting", "role:path"])
    assert tc.necessity_outcome(d(**(HIGH | {"necessity_confidence": "low"})), "flag", True) == ("create", [])
    assert tc.necessity_outcome(d(serves=[], necessity_confidence="high"), "flag", True) == ("suppress", [])
    assert tc.necessity_outcome(d(serves=[], necessity_confidence="medium"), "flag", True) == ("create", [])
    assert tc.necessity_outcome(d(serves=[], necessity_confidence="medium"), "suppress", True) == ("suppress", [])
    assert tc.necessity_outcome(d(serves=[], necessity_confidence="low"), "suppress", True) == ("create", [])
    assert tc.necessity_outcome(d(serves=[], necessity_confidence="high"), "suppress", False) == ("create", [])


def test_confident_serves_tags_at_creation_and_records_the_draft(monkeypatch):
    _strategy_on(monkeypatch)
    _mode(monkeypatch, "flag")
    _stub_triage(monkeypatch, Decision(**HIGH))
    seen = {}
    monkeypatch.setattr(tags, "resolve_gids", lambda names: (seen.setdefault("names", names), ["g1"])[1])
    _stub_enrichment(monkeypatch)
    inserts = _stub_db(monkeypatch)
    _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    task_create.handle(make_email_event())
    assert "serves:consulting" in seen["names"] and "role:path" in seen["names"]
    assert inserts[0]["serves_estimated"] == {"serves": ["consulting"], "role": "path", "confidence": "high"}


def test_high_confidence_none_never_becomes_a_task_in_flag_mode(monkeypatch):
    _strategy_on(monkeypatch)
    _mode(monkeypatch, "flag")
    _stub_triage(monkeypatch, Decision(serves=[], necessity_confidence="high", necessity_reason="newsletter"))
    rows = _stub_suppressions(monkeypatch)
    created = _capture_create(monkeypatch)
    task_create.handle(make_email_event(web_link="https://outlook/x"))
    assert created == []
    assert rows[0]["source"] == "necessity" and rows[0]["reason"] == "newsletter"
    assert rows[0]["web_link"] == "https://outlook/x"
    assert rows[0]["evidence"][0]["necessity_confidence"] == "high"


def test_medium_confidence_none_creates_in_flag_and_suppresses_in_suppress(monkeypatch):
    _strategy_on(monkeypatch)
    _stub_triage(monkeypatch, Decision(serves=[], necessity_confidence="medium"))
    _stub_enrichment(monkeypatch)
    _stub_db(monkeypatch)
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    _mode(monkeypatch, "flag")
    created = _capture_create(monkeypatch)
    task_create.handle(make_email_event())
    assert len(created) == 1
    _mode(monkeypatch, "suppress")
    rows = _stub_suppressions(monkeypatch)
    created = _capture_create(monkeypatch)
    task_create.handle(make_email_event())
    assert created == [] and rows[0]["source"] == "necessity"


def test_no_strategy_ignores_the_judgment_entirely(monkeypatch):
    _strategy_on(monkeypatch, on=False)
    _mode(monkeypatch, "suppress")
    _stub_triage(monkeypatch, Decision(serves=[], necessity_confidence="high"))
    _stub_enrichment(monkeypatch)
    inserts = _stub_db(monkeypatch)
    seen = {}
    monkeypatch.setattr(tags, "resolve_gids", lambda names: (seen.setdefault("names", names), [])[1])
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    created = _capture_create(monkeypatch)
    task_create.handle(make_email_event())
    assert len(created) == 1 and not any(n.startswith("serves:") for n in seen["names"])
    assert inserts[0]["serves_estimated"] is None
```

(`_capture_create` returns the list of captured create calls; `_stub_suppressions` returns the list of inserted rows; `_stub_db` returns the list of `repo_tasks.insert` kwargs — check the existing helpers' return values at the top of the file and adjust the assertions' variable names to match. `make_email_event` accepts keyword overrides; if it does not accept `web_link`, set `event["web_link"]` on the returned dict instead.)

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_task_create.py -q -k "necessity or confident or no_strategy"`
Expected: FAIL — `AttributeError: module 'handlers.task_create' has no attribute 'necessity_outcome'`.

- [ ] **Step 3: Implement**

In `handlers/task_create.py`:

```python
from models.events import CreatedTask, Decision, EmailClassifiedEvent, Screening
from services import prioritize_config, strategy as strategy_service


def necessity_outcome(decision: Decision, mode: str, has_strategy: bool) -> tuple[str, list[str]]:
    """Spec D14's table. ("tag", names) creates with serves/role tags;
    ("create", []) creates untagged and lets enrichment judge; ("suppress",
    []) records a necessity suppression and creates nothing."""
    if not has_strategy:
        return "create", []
    conf = decision.necessity_confidence
    if decision.serves:
        if conf in ("medium", "high"):
            roles = [s["role"] for s in decision.serves]
            role = max(roles, key=lambda r: {"path": 3, "derisk": 2, "support": 1}[r])
            return "tag", [f"serves:{s['goal']}" for s in decision.serves] + [f"role:{role}"]
        return "create", []
    if conf == "high" or (conf == "medium" and mode == "suppress"):
        return "suppress", []
    return "create", []
```

`_suppress` gains `web_link: str | None = None` and passes `web_link=web_link or event.get("web_link")` to `repo_suppressions.insert`. In `handle`, after the existing gate-2 suppression block:

```python
    strategy = strategy_service.load()
    mode = prioritize_config.load().necessity_mode
    outcome, tag_names = necessity_outcome(decision, mode, bool(strategy.goals))
    if outcome == "suppress":
        _suppress(
            event,
            reason=decision.necessity_reason or "serves no goal or area",
            source="necessity",
            related_task_gid=None,
            evidence=[{"kind": "strategy", "ref": "Strategy", "note": decision.necessity_reason,
                       "necessity_confidence": decision.necessity_confidence}],
        )
        return
    draft = None
    if outcome == "tag":
        draft = {"serves": [s["goal"] for s in decision.serves], "role": tag_names[-1].partition(":")[2],
                 "confidence": decision.necessity_confidence}
    create_from_event(event, verdict, extra_tags=tag_names, serves_estimated=draft)
```

Move everything from `# Enrichment: generated summary first` to the end of `handle` into:

```python
def create_from_event(
    event: EmailClassifiedEvent,
    verdict: Screening,
    *,
    extra_tags: list[str] | None = None,
    serves_estimated: dict | None = None,
) -> CreatedTask | None:
    """Everything after the gates: summary, deadline, tags, create, record,
    section, index, publish. Also the restore path's entry point (D14)."""
```

with two changes inside: `tags.resolve_gids([*shared_tags.for_event(...), *(extra_tags or [])])`, and `repo_tasks.insert(..., serves_estimated=serves_estimated)`. It returns `task`.

- [ ] **Step 4: Run the creation suite**

Run: `.venv/bin/pytest tests/test_task_create.py tests/test_main.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add handlers/task_create.py tests/test_task_create.py
git commit -m "feat(strategy): tag at creation; high-confidence 'serves nothing' never becomes a task"
```

---

### Task 11: Review builder, API — `/review`, reports, mute, restore, calibrate necessity

**Files:**
- Create: `services/review.py`
- Create: `api/routers/review.py`
- Modify: `api/routers/next.py` (`/calibrate`)
- Modify: `api/main.py`
- Test: `tests/test_review_service.py` (new), `tests/test_api_review.py` (new), `tests/test_api_next.py`

**Interfaces:**
- Consumes: `repo.goals.*` (including `load_snapshot` — the API never reads the secret), `repo.prioritize.list_scores`, `necessity_rows`, `repo.suppressions.list_necessity/get/mark_restored/restore_rates`, `services.strategy.load`, `asana.create_task_from_fields`, `asana.find_task_by_external`, `tags_service.resolve_gids`, `pubsub.publish_task_changed`, `task_index.refresh`.
- Produces: `services.review.build(strategy, states: dict[str, GoalState], scores: list[dict], suppressed: list[dict], today) -> dict` (the `GET /review` body, spec D11 shape); `services.review.render(review: dict) -> str` (markdown); `services.review.necessity_calibration(rows: list[dict], restore_rates: list[dict]) -> dict`; `api.routers.review.router` with `GET /review`, `POST /goals/{id}/reports`, `POST /goals/{id}/mute`, `POST /suppressions/{message_id}/restore`.

- [ ] **Step 1: Write the failing service tests**

```python
# tests/test_review_service.py
from datetime import date

from models.strategy import Goal, GoalState, Strategy
from services import review

TODAY = date(2026, 10, 9)
STRAT = Strategy(goals=(Goal(id="consulting", kind="outcome"), Goal(id="finances", kind="area")),
                 last_reviewed=date(2026, 9, 1), findings=("consulting: outcome goal has no tripwire",), text_hash="h")


def score(gid, name, bucket="next", **components):
    return {"task_gid": gid, "name": name, "bucket": bucket, "position": 1, "rank": None, "score": 1.0,
            "permalink_url": f"u/{gid}", "project_name": "Work",
            "components": {"serves": [], "role": None, "grooming": False, "confident_none": False, "reason": "r",
                           "necessity_confidence": "low"} | components}


def test_build_shapes_goals_grooming_and_stop_doing():
    states = {
        "consulting": GoalState("consulting", "outcome", TODAY, "h", {
            "next_step": "t1", "stalled": False, "leads": [{"tag": "conversation", "window": "week", "value": 1, "threshold": 3, "met": False}],
            "lag": None, "tripwires": [], "diagnosis": "insufficient data"}),
        "finances": GoalState("finances", "area", TODAY, "h", {
            "below_the_line": True, "muted_until": None,
            "signals": [{"signal": "overdue", "class": "evidence", "raw": True, "effective": True, "consecutive_days": 4, "state": "true", "tasks": ["t2"]}]}),
    }
    scores = [
        score("t1", "[P1] Write offer", serves=["consulting"], role="path"),
        score("t2", "[P1] Pay bill", serves=["finances"], role="derisk"),
        score("t3", "[P2] Maybe", grooming=True, necessity_confidence="low", serves_suggested=["consulting"]),
        score("t4", "[P3] Fluff", confident_none=True, necessity_confidence="high"),
        score("t5", "[P3] Gone", bucket="stop_doing", confident_none=True, necessity_confidence="high"),
    ]
    sup = [{"message_id": "m", "subject": "Newsletter", "sender": "x", "reason": "r", "web_link": "w", "created_at": None, "restored_at": None}]
    r = review.build(STRAT, states, scores, sup, TODAY)
    assert r["strategy_last_reviewed"] == "2026-09-01" and r["findings"] == ["consulting: outcome goal has no tripwire"]
    goals = {g["id"]: g for g in r["goals"]}
    assert goals["consulting"]["next_step"] == {"gid": "t1", "name": "[P1] Write offer", "permalink_url": "u/t1"}
    assert goals["consulting"]["stalled"] is False and goals["consulting"]["diagnosis"] == "insufficient data"
    assert goals["finances"]["below_the_line"] is True
    assert goals["finances"]["signals"][0]["tasks"] == [{"gid": "t2", "name": "[P1] Pay bill", "permalink_url": "u/t2"}]
    assert [g["gid"] for g in r["grooming"]] == ["t3"]
    assert [g["gid"] for g in r["stop_doing"]["tasks"]] == ["t4", "t5"]
    assert r["stop_doing"]["suppressed_emails"][0]["subject"] == "Newsletter"


def test_render_is_markdown_with_links():
    r = review.build(STRAT, {}, [score("t1", "[P1] Write offer", serves=["consulting"], role="path")], [], TODAY)
    md = review.render(r)
    assert md.startswith("# Weekly strategy review — 2026-10-09")
    assert "## consulting" in md and "stalled" in md
    assert "outcome goal has no tripwire" in md


def test_necessity_calibration_groups_by_band_and_source():
    rows = [
        {"task_gid": "a", "serves_estimated": {"serves": ["consulting"], "role": "path", "confidence": "high"}, "tags": ["serves:consulting", "role:path"], "overrides": None, "source": "enrichment"},
        {"task_gid": "b", "serves_estimated": {"serves": ["consulting"], "role": "path", "confidence": "high"}, "tags": ["serves:finances", "role:path"], "overrides": None, "source": "enrichment"},
        {"task_gid": "c", "serves_estimated": {"grooming": True}, "tags": ["serves:finances"], "overrides": None, "source": "enrichment"},
        {"task_gid": "d", "serves_estimated": {"grooming": True}, "tags": [], "overrides": None, "source": "enrichment"},
        {"task_gid": "e", "serves_estimated": {"serves": ["finances"], "role": "support", "confidence": "medium"}, "tags": ["serves:finances", "role:support"], "overrides": None, "source": "gate2"},
    ]
    rates = [{"band": "high", "restored": 1, "settled": 3, "pending": 2}]
    c = review.necessity_calibration(rows, rates)
    assert c["by_confidence"]["high"] == {"judged": 2, "agreed": 1, "rate": 0.5}
    assert c["by_source"]["gate2"] == {"judged": 1, "agreed": 1, "rate": 1.0}
    assert c["grooming"] == {"attached": 1, "unresolved": 1}
    assert c["suppressions"]["high"] == {"restored": 1, "settled": 3, "pending": 2, "restore_rate": 0.25}
```

- [ ] **Step 2: Write the failing API tests**

```python
# tests/test_api_review.py
from datetime import date

import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
import clients.pubsub as ps
from api.main import app
from api.routers import review as review_router
from models.strategy import Goal, Strategy
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import task_index

client = TestClient(app)
AUTH = {"Authorization": "Bearer x"}


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


@pytest.fixture(autouse=True)
def wiring(monkeypatch):
    monkeypatch.setattr(review_router, "get_conn", lambda: Conn())
    monkeypatch.setattr(repo_goals, "load_snapshot", lambda c: Strategy(goals=(Goal(id="consulting", kind="outcome"), Goal(id="finances", kind="area")), text_hash="h"))
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {})
    monkeypatch.setattr(repo, "list_scores", lambda c: [])
    monkeypatch.setattr(repo_sup, "list_necessity", lambda c, limit=100: [])
    monkeypatch.setattr(review_router, "_today", lambda: date(2026, 10, 9))


def test_get_review_shape():
    r = client.get("/review", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert {g["id"] for g in body["goals"]} == {"consulting", "finances"}
    assert body["grooming"] == [] and body["stop_doing"] == {"tasks": [], "suppressed_emails": []}


def test_post_report_validates_goal_and_writes(monkeypatch):
    written = []
    monkeypatch.setattr(repo_goals, "insert_report", lambda c, g, v, ps: (written.append((g, v, ps)), 3)[1])
    r = client.post("/goals/consulting/reports", json={"value": 4200, "period_start": "2026-10-01"}, headers=AUTH)
    assert r.status_code == 201 and r.json() == {"id": 3, "goal_id": "consulting", "value": 4200.0, "period_start": "2026-10-01"}
    assert written == [("consulting", 4200.0, date(2026, 10, 1))]
    assert client.post("/goals/ghost/reports", json={"value": 1}, headers=AUTH).status_code == 404
    assert client.post("/goals/finances/reports", json={"value": 1}, headers=AUTH).status_code == 400  # areas have no lag


def test_post_mute_sets_and_clears(monkeypatch):
    calls = []
    monkeypatch.setattr(repo_goals, "set_mute", lambda c, g, until: calls.append((g, until)))
    assert client.post("/goals/finances/mute", json={"until": "2026-10-20"}, headers=AUTH).status_code == 200
    assert client.post("/goals/finances/mute", json={"until": None}, headers=AUTH).status_code == 200
    assert calls == [("finances", date(2026, 10, 20)), ("finances", None)]
    assert client.post("/goals/consulting/mute", json={"until": "2026-10-20"}, headers=AUTH).status_code == 400


def test_restore_creates_once_and_marks(monkeypatch):
    row = {"message_id": "m1", "category": "review", "importance": "P2", "subject": "Newsletter", "sender": "x@y",
           "reason": "serves nothing", "source": "necessity", "web_link": "https://outlook/m1", "restored_at": None,
           "restored_task_gid": None, "created_at": None}
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: dict(row))
    marked = []
    monkeypatch.setattr(repo_sup, "mark_restored", lambda c, mid, gid: (marked.append((mid, gid)), True)[1])
    created = []
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: None)
    monkeypatch.setattr(asana, "create_task_from_fields", lambda f: (created.append(f), type("T", (), {"gid": "t9", "permalink_url": "u"})())[1])
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj")
    monkeypatch.setattr(review_router.tags_service, "resolve_gids", lambda names: [])
    monkeypatch.setattr(task_index, "refresh", lambda gid: None)
    monkeypatch.setattr(ps, "publish_task_changed", lambda gid, source: None)
    r = client.post("/suppressions/m1/restore", headers=AUTH)
    assert r.status_code == 201 and r.json() == {"task_gid": "t9", "permalink_url": "u", "message_id": "m1"}
    assert created[0]["name"] == "[P2] Newsletter" and created[0]["external"] == {"gid": "m1", "data": "inbox"}
    assert "https://outlook/m1" in created[0]["html_notes"] and marked == [("m1", "t9")]
    # second call: already restored
    row["restored_at"], row["restored_task_gid"] = "2026-10-09", "t9"
    r = client.post("/suppressions/m1/restore", headers=AUTH)
    assert r.status_code == 200 and r.json()["task_gid"] == "t9" and len(created) == 1


def test_restore_unknown_or_non_necessity_is_404(monkeypatch):
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: None)
    assert client.post("/suppressions/zz/restore", headers=AUTH).status_code == 404
    monkeypatch.setattr(repo_sup, "get", lambda c, mid: {"message_id": "m", "source": "agent", "restored_at": None})
    assert client.post("/suppressions/m/restore", headers=AUTH).status_code == 404
```

```python
# append to tests/test_api_next.py
def test_calibrate_has_necessity_section(monkeypatch):
    monkeypatch.setattr(repo, "calibration_rows", lambda c: [])
    monkeypatch.setattr(repo, "necessity_rows", lambda c: [])
    from repo import tasks as repo_tasks
    from repo import suppressions as repo_sup
    monkeypatch.setattr(repo_tasks, "necessity_rows", lambda c: [])
    monkeypatch.setattr(repo_sup, "restore_rates", lambda c, settle: [])
    body = client.get("/calibrate", headers=AUTH).json()
    assert body["necessity"] == {"by_confidence": {}, "by_source": {}, "by_strategy": {}, "grooming": {"attached": 0, "unresolved": 0}, "suppressions": {}}
```

(Find how `tests/test_api_next.py` patches `get_conn` for its existing `/calibrate` test and reuse that fixture.)

- [ ] **Step 3: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_review_service.py tests/test_api_review.py tests/test_api_next.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.review'`, 404s on the new routes.

- [ ] **Step 4: `services/review.py`**

```python
"""The weekly review: a pure builder over stored rows (spec D11) and a
markdown renderer, plus the necessity half of calibrate (D10)."""

from datetime import date

from models.strategy import GoalState, Strategy


def _ref(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {"gid": row["task_gid"], "name": row["name"], "permalink_url": row.get("permalink_url")}


def build(strategy: Strategy, states: dict[str, GoalState], scores: list[dict], suppressed: list[dict], today: date) -> dict:
    by_gid = {r["task_gid"]: r for r in scores}
    goals = []
    for g in strategy.goals:
        s = (states.get(g.id).state if states.get(g.id) else {}) or {}
        entry: dict = {"id": g.id, "kind": g.kind, "weight": g.weight,
                       "next_step": _ref(by_gid.get(s.get("next_step"))), "stalled": s.get("next_step") is None}
        if g.kind == "outcome":
            entry.update({"horizon": g.horizon.isoformat() if g.horizon else None,
                          "leads": s.get("leads", []), "lag": s.get("lag"), "tripwires": s.get("tripwires", []),
                          "diagnosis": s.get("diagnosis", "insufficient data")})
        else:
            entry.update({"below_the_line": bool(s.get("below_the_line")), "muted_until": s.get("muted_until"),
                          "signals": [sig | {"tasks": [_ref(by_gid.get(t)) or {"gid": t, "name": t, "permalink_url": None}
                                                       for t in sig.get("tasks", [])]}
                                      for sig in s.get("signals", [])]})
        goals.append(entry)
    active = [r for r in scores if r["bucket"] in ("next", "nudge", "stop_doing")]
    grooming = [
        _ref(r) | {"serves_suggested": r["components"].get("serves_suggested") or r["components"].get("serves") or [],
                   "confidence": r["components"].get("necessity_confidence"), "reason": r["components"].get("reason")}
        for r in active if r["components"].get("grooming")
    ]
    stop = [_ref(r) | {"reason": r["components"].get("reason")} for r in active if r["components"].get("confident_none")]
    return {
        "reviewed_at": today.isoformat(),
        "strategy_last_reviewed": strategy.last_reviewed.isoformat() if strategy.last_reviewed else None,
        "findings": list(strategy.findings),
        "goals": goals,
        "grooming": grooming,
        "stop_doing": {
            "tasks": stop,
            "suppressed_emails": [
                {"message_id": r["message_id"], "subject": r.get("subject"), "sender": r.get("sender"),
                 "web_link": r.get("web_link"), "reason": r.get("reason"),
                 "created_at": r["created_at"].isoformat() if hasattr(r.get("created_at"), "isoformat") else r.get("created_at"),
                 "restored": r.get("restored_at") is not None}
                for r in suppressed
            ],
        },
    }


def _link(ref: dict | None) -> str:
    if not ref:
        return "—"
    return f"[{ref['name']}]({ref['permalink_url']})" if ref.get("permalink_url") else ref["name"]


def render(review: dict) -> str:
    out = [f"# Weekly strategy review — {review['reviewed_at']}", ""]
    if review["findings"]:
        out += ["**Findings**"] + [f"- {f}" for f in review["findings"]] + [""]
    for g in review["goals"]:
        out.append(f"## {g['id']} ({g['kind']})")
        out.append(f"- next step: {_link(g['next_step'])}" + (" — **stalled**" if g["stalled"] else ""))
        if g["kind"] == "outcome":
            for lead in g["leads"]:
                out.append(f"- lead {lead['tag']}: {lead['value']} / {lead['threshold']:g} per {lead['window']} — {'met' if lead['met'] else 'not met'}")
            lag = g["lag"]
            out.append(f"- lag: {lag['value']:g} / {lag['threshold']:g} — {'met' if lag['met'] else 'not met'}" if lag else "- lag: not reported")
            for t in g["tripwires"]:
                out.append(f"- tripwire {t['text']} → {t['action']}: {'**FIRED**' if t['fired'] else ('watching' if t['evaluated'] else 'not yet due')}")
            out.append(f"- diagnosis: **{g['diagnosis']}**")
        else:
            out.append(f"- below the line: {'**yes**' if g['below_the_line'] else 'no'}" + (f" (muted until {g['muted_until']})" if g["muted_until"] else ""))
            for s in g["signals"]:
                tasks = ", ".join(_link(t) for t in s["tasks"]) or "—"
                out.append(f"  - {s['signal']} [{s['class']}]: {s['state']} ({s['consecutive_days']}d) — {tasks}")
        out.append("")
    out.append("## Grooming")
    out += [f"- {_link(t)} — suggested {', '.join(t['serves_suggested']) or 'nothing'} ({t['confidence']}): {t['reason']}" for t in review["grooming"]] or ["- nothing"]
    out += ["", "## Stop doing"]
    out += [f"- {_link(t)} — {t['reason']} (remove, or attach with a serves: tag)" for t in review["stop_doing"]["tasks"]] or ["- nothing"]
    for e in review["stop_doing"]["suppressed_emails"]:
        flag = " — restored" if e["restored"] else ""
        out.append(f"- email [{e['subject']}]({e['web_link']}) from {e['sender']} — {e['reason']}{flag}")
    return "\n".join(out) + "\n"


def _effective(row: dict) -> tuple[list[str], str | None]:
    tags = row.get("tags") or []
    serves = [t.partition(":")[2] for t in tags if str(t).casefold().startswith("serves:")]
    role = next((t.partition(":")[2] for t in tags if str(t).casefold().startswith("role:")), None)
    if not serves and row.get("overrides"):
        serves = list((row["overrides"] or {}).get("serves") or [])
        role = (row["overrides"] or {}).get("role") or role
    return sorted(serves), role


def necessity_calibration(rows: list[dict], restore_rates: list[dict]) -> dict:
    by_conf: dict[str, dict] = {}
    by_source: dict[str, dict] = {}
    by_strategy: dict[str, dict] = {}
    grooming = {"attached": 0, "unresolved": 0}

    def bump(bucket: dict, key: str, agreed: bool) -> None:
        b = bucket.setdefault(key, {"judged": 0, "agreed": 0})
        b["judged"] += 1
        b["agreed"] += int(agreed)

    for r in rows:
        est = r.get("serves_estimated") or {}
        serves, role = _effective(r)
        if est.get("grooming"):
            grooming["attached" if serves else "unresolved"] += 1
            continue
        if est.get("none"):
            agreed = not serves
            conf = est.get("confidence", "unknown")
        else:
            agreed = sorted(est.get("serves") or []) == serves and est.get("role") == role
            conf = est.get("confidence", "unknown")
        bump(by_conf, conf, agreed)
        bump(by_source, r.get("source") or "enrichment", agreed)
        if r.get("strategy_hash"):
            bump(by_strategy, r["strategy_hash"], agreed)
    for bucket in (by_conf, by_source, by_strategy):
        for b in bucket.values():
            b["rate"] = b["agreed"] / b["judged"] if b["judged"] else None
    sup = {}
    for r in restore_rates:
        decided = int(r["restored"]) + int(r["settled"])
        sup[r["band"]] = {"restored": int(r["restored"]), "settled": int(r["settled"]), "pending": int(r["pending"]),
                          "restore_rate": (int(r["restored"]) / decided) if decided else None}
    return {"by_confidence": by_conf, "by_source": by_source, "by_strategy": by_strategy, "grooming": grooming, "suppressions": sup}
```

`repo.prioritize.necessity_rows` (Task 5) must also select `e.strategy_hash` via `LEFT JOIN task_enrichment e USING (task_gid)` and add a constant `'enrichment' AS source`; add `repo.tasks.necessity_rows(conn)` selecting `task_gid, serves_estimated, 'gate2' AS source` from `tasks WHERE serves_estimated IS NOT NULL` joined to `task_facts` for `tags` and to `task_overrides` for `overrides`.

- [ ] **Step 5: `api/routers/review.py`**

```python
"""Strategy read side and its three small writes: the weekly review, lag
reports, area mutes, and restoring a necessity suppression (spec D11, D12,
D14). Reads goal_state/task_scores only; the restore is the one Asana write."""

from datetime import date

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

import clients.asana as asana
import clients.pubsub as pubsub
from clients.db import get_conn
from models.strategy import Strategy
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import review, task_index
from services import tags as tags_service
from services.due_digest import today_local

router = APIRouter()


def _today() -> date:
    return today_local()


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    period_start: date | None = None


class MuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: date | None


def _strategy() -> Strategy:
    """The daily tick's snapshot (spec D1): this service reads rows, never
    the mounted secret."""
    with get_conn() as conn:
        return repo_goals.load_snapshot(conn)


@router.get("/review")
def get_review() -> dict:
    strategy = _strategy()
    today = _today()
    with get_conn() as conn:
        states = repo_goals.get_states(conn, today)
        if not states:  # before today's tick: show yesterday's
            states = repo_goals.get_states(conn, today.fromordinal(today.toordinal() - 1))
        scores = repo.list_scores(conn)
        suppressed = repo_sup.list_necessity(conn)
    return review.build(strategy, states, scores, suppressed, today)


@router.post("/goals/{goal_id}/reports", status_code=201)
def post_report(goal_id: str, body: ReportRequest) -> dict:
    goal = _strategy().get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail=f"unknown goal: {goal_id}")
    if goal.kind != "outcome":
        raise HTTPException(status_code=400, detail="only an outcome goal has a lag measure")
    with get_conn() as conn:
        rid = repo_goals.insert_report(conn, goal_id, float(body.value), body.period_start)
    return {"id": rid, "goal_id": goal_id, "value": float(body.value),
            "period_start": body.period_start.isoformat() if body.period_start else None}


@router.post("/goals/{goal_id}/mute")
def post_mute(goal_id: str, body: MuteRequest) -> dict:
    goal = _strategy().get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail=f"unknown goal: {goal_id}")
    if goal.kind != "area":
        raise HTTPException(status_code=400, detail="only an area can be muted")
    with get_conn() as conn:
        repo_goals.set_mute(conn, goal_id, body.until)
    return {"goal_id": goal_id, "mute_until": body.until.isoformat() if body.until else None}


@router.post("/suppressions/{message_id}/restore", status_code=201)
def restore(message_id: str, response: Response) -> dict:
    with get_conn() as conn:
        row = repo_sup.get(conn, message_id)
    if row is None or row.get("source") != "necessity":
        raise HTTPException(status_code=404, detail="no necessity suppression for that message")
    if row.get("restored_at"):
        response.status_code = 200
        return {"task_gid": row["restored_task_gid"], "permalink_url": None, "message_id": message_id,
                "already_restored": True}
    existing = asana.find_task_by_external(message_id)
    if existing:
        gid, url = existing, None
    else:
        subject = row.get("subject") or "(no subject)"
        fields = {
            "name": f"[{row.get('importance') or 'P2'}] {subject}",
            "html_notes": (
                f"<body>Restored from a necessity suppression. From {row.get('sender') or '?'}.\n"
                f"Suppressed because: {row.get('reason') or '—'}\n"
                f"<a href=\"{row.get('web_link') or ''}\">Open the email</a></body>"
            ),
            "projects": [asana.ASANA_PROJECT_ID],
            "external": {"gid": message_id, "data": "inbox"},
        }
        tag_gids = tags_service.resolve_gids(["restored"])
        if tag_gids:
            fields["tags"] = tag_gids
        created = asana.create_task_from_fields(fields)
        gid, url = created.gid, created.permalink_url
    with get_conn() as conn:
        repo_sup.mark_restored(conn, message_id, gid)
    task_index.refresh(gid)
    pubsub.publish_task_changed(gid, "api")
    return {"task_gid": gid, "permalink_url": url, "message_id": message_id}
```

Register in `api/main.py`: `from api.routers import review as review_router` and `app.include_router(review_router.router)`.

`api/routers/next.py::calibrate` gains:

```python
    from repo import suppressions as repo_sup
    from repo import tasks as repo_tasks
    from services import review as review_service
    cfg = prioritize_config.load()
    with get_conn() as conn:
        rows = repo.calibration_rows(conn)
        nrows = repo.necessity_rows(conn) + repo_tasks.necessity_rows(conn)
        rates = repo_sup.restore_rates(conn, cfg.suppression_settle_days)
    ...
    return {"projects": [...], "overall": summarise(rows),
            "necessity": review_service.necessity_calibration(nrows, rates)}
```

(move the imports to the top of the module).

- [ ] **Step 6: Run the tests**

Run: `.venv/bin/pytest tests/test_review_service.py tests/test_api_review.py tests/test_api_next.py tests/test_api_main.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add services/review.py api/routers/review.py api/routers/next.py api/main.py repo/prioritize.py repo/tasks.py tests/test_review_service.py tests/test_api_review.py tests/test_api_next.py
git commit -m "feat(strategy): GET /review, lag reports, area mute, suppression restore, calibrate necessity"
```

---

### Task 12: Weekly review as a task, the webhook route, the scheduler, the mount

**Files:**
- Create: `handlers/weekly_review.py`
- Modify: `main.py` (webhook route `/review`)
- Modify: `terraform/cloud_functions.tf` (mount on `tasks_prioritize`), `terraform/scheduler.tf`
- Test: `tests/test_weekly_review.py` (new), `tests/test_main.py`

**Interfaces:**
- Consumes: `services.review.build/render`, `repo.goals.load_snapshot/get_states`, `repo.prioritize.list_scores`, `repo.suppressions.list_necessity`, `asana.find_task_by_external`, `asana.create_task_from_fields`, `asana.create_story`, `sections.for_category("review")`, `escalation.is_authorized`.
- Produces: `handlers.weekly_review.run() -> dict` (`{"outcome": "posted"|"no_strategy"|"db_unavailable", "task_gid"?: str}`); `REVIEW_EXTERNAL = "review:weekly"`; `REVIEW_TASK_NAME = "[P2] Weekly strategy review"`; `POST /review` on the webhook CF (escalate bearer).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_weekly_review.py
from datetime import date

import pytest

import clients.asana as asana
from handlers import weekly_review as wr
from models.strategy import Goal, Strategy
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup


class Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(wr, "get_conn", lambda: Conn())
    monkeypatch.setattr(wr, "today_local", lambda: date(2026, 10, 12))
    monkeypatch.setattr(repo_goals, "load_snapshot", lambda c: Strategy(goals=(Goal(id="consulting", kind="outcome"),), text_hash="h"))
    monkeypatch.setattr(repo_goals, "get_states", lambda c, day: {})
    monkeypatch.setattr(repo, "list_scores", lambda c: [])
    monkeypatch.setattr(repo_sup, "list_necessity", lambda c, limit=100: [])
    calls = {"created": [], "stories": [], "existing": None}
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: calls["existing"])
    monkeypatch.setattr(asana, "create_task_from_fields", lambda f: (calls["created"].append(f), type("T", (), {"gid": "rv1", "permalink_url": "u"})())[1])
    monkeypatch.setattr(asana, "create_story", lambda gid, text=None, html_text=None: calls["stories"].append((gid, text)))
    monkeypatch.setattr(asana, "add_task_to_section", lambda gid, sec: None)
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj")
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec")
    return calls


def test_run_creates_the_standing_task_once_and_posts_the_review(wired):
    out = wr.run()
    assert out == {"outcome": "posted", "task_gid": "rv1"}
    assert wired["created"][0]["name"] == wr.REVIEW_TASK_NAME
    assert wired["created"][0]["external"] == {"gid": wr.REVIEW_EXTERNAL, "data": "tasks"}
    gid, text = wired["stories"][0]
    assert gid == "rv1" and text.startswith("# Weekly strategy review — 2026-10-12")
    wired["existing"] = "rv1"
    wr.run()
    assert len(wired["created"]) == 1 and len(wired["stories"]) == 2


def test_run_without_strategy_posts_nothing(wired, monkeypatch):
    monkeypatch.setattr(repo_goals, "load_snapshot", lambda c: Strategy.EMPTY)
    assert wr.run() == {"outcome": "no_strategy"} and wired["stories"] == []


def test_run_db_unavailable_is_reported_not_raised(wired, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(wr, "get_conn", boom)
    assert wr.run() == {"outcome": "db_unavailable"}
```

```python
# append to tests/test_main.py (mirror the existing /digest route test's shape)
def test_review_route_requires_bearer_and_runs(monkeypatch):
    from handlers import weekly_review
    monkeypatch.setenv("ASANA_ESCALATE_TOKEN", "tok")
    monkeypatch.setattr(weekly_review, "run", lambda: {"outcome": "posted"})
    assert main.webhook(_req("/review", "POST", headers={}))[1] == 401
    body, status = main.webhook(_req("/review", "POST", headers={"Authorization": "Bearer tok"}))
    assert status == 200 and body == {"outcome": "posted"}
```

(`_req` is whatever request-builder helper `tests/test_main.py` already uses for `/digest`; copy its signature.)

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_weekly_review.py tests/test_main.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.weekly_review'`; the route returns 405.

- [ ] **Step 3: Implement the handler**

```python
# handlers/weekly_review.py
"""Monday's review (spec D11): build GET /review's body from the stored
rows, render it, and post it as a comment on one standing task so the
cadence lives in the list. Best-effort on the DB like the digest: without
rows there is nothing to say, so a DB failure reports and does not raise."""

import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import review, sections
from services.due_digest import today_local

logger = logging.getLogger(__name__)

REVIEW_EXTERNAL = "review:weekly"
REVIEW_TASK_NAME = "[P2] Weekly strategy review"


def _standing_task() -> str:
    gid = asana.find_task_by_external(REVIEW_EXTERNAL)
    if gid:
        return gid
    created = asana.create_task_from_fields(
        {
            "name": REVIEW_TASK_NAME,
            "html_notes": "<body>The weekly strategy review lands here as a comment every Monday. "
            "Never complete this task; it is the review's home.</body>",
            "projects": [asana.ASANA_PROJECT_ID],
            "external": {"gid": REVIEW_EXTERNAL, "data": "tasks"},
        }
    )
    section = sections.for_category("review")
    if section:
        asana.add_task_to_section(created.gid, section)
    return created.gid


def run() -> dict:
    today = today_local()
    try:
        with get_conn() as conn:
            strategy = repo_goals.load_snapshot(conn)
            states = repo_goals.get_states(conn, today)
            if not states:
                states = repo_goals.get_states(conn, today.fromordinal(today.toordinal() - 1))
            scores = repo.list_scores(conn)
            suppressed = repo_sup.list_necessity(conn)
    except Exception:
        logger.exception("weekly review: DB unavailable — skipping")
        otel.errors.add(1, {"handler": "weekly_review"})
        return {"outcome": "db_unavailable"}
    if not strategy.goals:
        return {"outcome": "no_strategy"}
    body = review.build(strategy, states, scores, suppressed, today)
    gid = _standing_task()
    asana.create_story(gid, text=review.render(body))
    logger.info("weekly review posted on %s (%d goals, %d grooming, %d stop-doing)",
                gid, len(body["goals"]), len(body["grooming"]), len(body["stop_doing"]["tasks"]))
    return {"outcome": "posted", "task_gid": gid}
```

In `main.py`, import `weekly_review` beside `webhook_sync` and add, after the `/digest` block:

```python
        if request.path == "/review" and request.method == "POST":
            if not escalation.is_authorized(request.headers.get("Authorization")):
                return "", 401
            return weekly_review.run(), 200
```

Update the module docstring's route list.

- [ ] **Step 4: Terraform**

In `terraform/cloud_functions.tf`, inside `tasks_prioritize`'s `service_config`, replace `environment_variables = local.common_env` with the same `merge(local.common_env, { STANDING_CONTEXT_PATH = "/etc/context/standing-context.md" })` block the events function uses, and add the identical `secret_volumes { mount_path = "/etc/context" ... }` block (copy it verbatim from `tasks_events`, lines 116–124). Grant the prioritize service account read on the secret the same way the events SA has it (look for `standing-context` in `terraform/iam.tf` or `secrets.tf` and add `google_service_account.tasks_prioritize_cf.email` as a member).

In `terraform/scheduler.tf`, append:

```hcl
# ---------------------------------------------------------------------------
# Weekly strategy review — Monday 07:00 local. Posts GET /review's body as a
# comment on the standing review task (strategy-layer spec D11).
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "weekly_review" {
  name      = "tasks-weekly-review"
  schedule  = "0 7 * * 1"
  time_zone = "America/New_York"

  attempt_deadline = "300s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloudfunctions2_function.tasks_webhook.service_config[0].uri}/review"
    body        = base64encode("{}")
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${var.tasks_escalate_token}"
    }
  }
}
```

Run `/terraform-plan` and confirm the plan shows exactly: one scheduler job added, the prioritize function updated in place (env + volume), one IAM binding added. Do not apply in this task.

- [ ] **Step 5: Run the tests**

Run: `.venv/bin/pytest tests/test_weekly_review.py tests/test_main.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add handlers/weekly_review.py main.py terraform/cloud_functions.tf terraform/scheduler.tf terraform/*.tf tests/test_weekly_review.py tests/test_main.py
git commit -m "feat(strategy): weekly review posts to a standing task; scheduler; strategy mount on tasks-prioritize"
```

---

### Task 13: Agents, skill, CLI, example document, CLAUDE.md, dry-run script

**Files:**
- Modify: `.claude/agents/task-next.md`, `.claude/agents/task-builder.md`, `.claude/skills/prioritizing-tasks/SKILL.md`
- Modify: `scripts/task_next.py`
- Modify: `context/standing-context.example.md`, `CLAUDE.md`
- Create: `scripts/test-review.py`
- Test: `tests/test_task_next.py`

**Interfaces:**
- Produces: `task-next review`, `task-next report <goal> <value> [--period-start YYYY-MM-DD]`, `task-next mute <area> <until|clear>`, `task-next restore <message_id>`; `scripts/test-review.py [--dry-run]`.

- [ ] **Step 1: Write the failing CLI tests**

```python
# append to tests/test_task_next.py (reuse the module's existing _api capture fixture pattern)
def test_review_renders_goals_and_lists(monkeypatch, capsys):
    payload = {"reviewed_at": "2026-10-12", "strategy_last_reviewed": "2026-10-01", "findings": ["f1"],
            "goals": [{"id": "consulting", "kind": "outcome", "next_step": {"gid": "1234567", "name": "[P1] Write offer", "permalink_url": "u"},
                       "stalled": False, "leads": [{"tag": "conversation", "window": "week", "value": 1, "threshold": 3, "met": False}],
                       "lag": None, "tripwires": [], "diagnosis": "insufficient data"},
                      {"id": "finances", "kind": "area", "below_the_line": True, "muted_until": None, "signals": [], "next_step": None, "stalled": True}],
            "grooming": [], "stop_doing": {"tasks": [], "suppressed_emails": []}}
    monkeypatch.setattr(task_next, "_api", lambda method, path, body=None, params=None: payload)
    assert task_next.main(["review"]) == 0
    out = capsys.readouterr().out
    assert "consulting" in out and "Write offer" in out and "below the line" in out and "f1" in out


def test_report_and_mute_post(monkeypatch):
    calls = []
    monkeypatch.setattr(task_next, "_api", lambda method, path, body=None, params=None: (calls.append((method, path, body)), {})[1])
    assert task_next.main(["report", "consulting", "4200", "--period-start", "2026-10-01"]) == 0
    assert task_next.main(["mute", "finances", "2026-10-20"]) == 0
    assert task_next.main(["mute", "finances", "clear"]) == 0
    assert calls == [
        ("POST", "/goals/consulting/reports", {"value": 4200.0, "period_start": "2026-10-01"}),
        ("POST", "/goals/finances/mute", {"until": "2026-10-20"}),
        ("POST", "/goals/finances/mute", {"until": None}),
    ]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_task_next.py -q -k "review or report_and_mute"`
Expected: FAIL — argparse exits with "invalid choice: 'review'".

- [ ] **Step 3: CLI**

In `scripts/task_next.py::main`, add parsers:

```python
    sub.add_parser("review")
    p = sub.add_parser("report")
    p.add_argument("goal")
    p.add_argument("value", type=float)
    p.add_argument("--period-start")
    p = sub.add_parser("mute")
    p.add_argument("area")
    p.add_argument("until")  # YYYY-MM-DD or "clear"
    sub.add_parser("restore").add_argument("message_id")
```

and handlers:

```python
    if args.cmd == "review":
        print(render_review(_api("GET", "/review")))
        return 0
    if args.cmd == "report":
        body = {"value": args.value, "period_start": args.period_start}
        print(json.dumps(_api("POST", f"/goals/{args.goal}/reports", body)))
        return 0
    if args.cmd == "mute":
        until = None if args.until == "clear" else args.until
        print(json.dumps(_api("POST", f"/goals/{args.area}/mute", {"until": until})))
        return 0
    if args.cmd == "restore":
        print(json.dumps(_api("POST", f"/suppressions/{args.message_id}/restore")))
        return 0
```

with:

```python
def render_review(r: dict) -> str:
    out = [f"# strategy review {r['reviewed_at']} — last reviewed {r.get('strategy_last_reviewed') or '—'}"]
    out += [f"! {f}" for f in r.get("findings") or []]
    for g in r["goals"]:
        head = f"## {g['id']} ({g['kind']})"
        if g["kind"] == "outcome":
            step = g["next_step"]
            out.append(f"{head} — {g['diagnosis']}")
            out.append("  next: " + (f"{task_ref.ref(step['gid'])}\t{step['gid']}\t{step['name']}" if step else "STALLED"))
            for lead in g["leads"]:
                out.append(f"  lead {lead['tag']}: {lead['value']}/{lead['threshold']:g} per {lead['window']} {'ok' if lead['met'] else 'LOW'}")
            lag = g["lag"]
            out.append(f"  lag: {lag['value']:g}/{lag['threshold']:g} {'ok' if lag['met'] else 'LOW'}" if lag else "  lag: unreported")
            for t in g["tripwires"]:
                out.append(f"  tripwire {t['text']}: {'FIRED' if t['fired'] else ('watching' if t['evaluated'] else 'pending')}")
        else:
            out.append(f"{head} — {'BELOW THE LINE' if g['below_the_line'] else 'ok'}" + (f" (muted until {g['muted_until']})" if g.get("muted_until") else ""))
            for s in g.get("signals") or []:
                out.append(f"  {s['signal']}: {s['state']} ({s['consecutive_days']}d) " + ", ".join(t['gid'] for t in s.get("tasks") or []))
    out.append("## grooming")
    out += [f"  {task_ref.ref(t['gid'])}\t{t['gid']}\t{t['name']}\t{', '.join(t['serves_suggested']) or '—'} ({t['confidence']})" for t in r["grooming"]] or ["  —"]
    out.append("## stop doing")
    out += [f"  {task_ref.ref(t['gid'])}\t{t['gid']}\t{t['name']}\t{t['reason']}" for t in r["stop_doing"]["tasks"]] or ["  —"]
    out += [f"  email\t{e['message_id']}\t{e['subject']}\t{e['reason']}" for e in r["stop_doing"]["suppressed_emails"]]
    return "\n".join(out)
```

(`task_ref` is already imported by the script for refs; if `render_review` is the first use, `import task_ref` beside the existing imports.)

- [ ] **Step 4: Agent, skill and docs**

`.claude/agents/task-next.md` — under **Reads** add:

```
- "weekly review / how are my goals / what's below the line / what's stalled" → `GET /review` (or `task-next review`). Report per goal: next step (or STALLED), leads vs threshold, lag, tripwires fired, diagnosis; per area: below the line and which tasks made it so; then grooming and stop-doing. A grooming task needs a `serves:` tag or removal — hand both to `editing-tasks`.
```

under **Writes (only these)** add:

```
- "revenue was 4200 this month / report 4200 for consulting" → `POST /goals/consulting/reports {"value": 4200, "period_start": "<first of month>"}`.
- "mute finances till the 20th / I'm away, quiet the home area" → `POST /goals/{area}/mute {"until": "YYYY-MM-DD"}`; "unmute" → `{"until": null}`.
- "bring that email back / restore that suppressed newsletter" → `POST /suppressions/{message_id}/restore` (message ids come from the review's stop-doing list).
```

and under the explanation list add: "**Necessity.** Every task carries `serves:<goal>` and `role:path|derisk|support` tags; `components.N` is the necessity term, `grooming` means the model was unsure, `confident_none` means it is sure the task serves nothing. In `flag` mode necessity does not move the ranking yet."

`.claude/skills/prioritizing-tasks/SKILL.md` — add the four curl lines (`GET /review`, `POST /goals/{id}/reports`, `POST /goals/{id}/mute`, `POST /suppressions/{message_id}/restore`) to the Endpoints block, the CLI forms `task-next review|report|mute|restore` to the CLI line, and a **Strategy** bullet under Meaning stating the tag families, the three roles, the grooming/stop-doing lists, and that signals debounce over three days.

`.claude/agents/task-builder.md` — in its tagging rule add: "Read the `## Strategy` section of the standing context if it is present in your context (`task-ref` cannot fetch it; ask the user when unsure). Every task you create carries exactly one `role:` tag (`path`, `derisk` or `support`) and one `serves:<id>` tag per goal or area it serves. If it serves nothing you can name, say so in the task's Context and add no `serves:` tag."

`context/standing-context.example.md` — append:

```markdown
## Strategy

- last reviewed: 2026-01-01

### example-goal
- kind: outcome
- weight: 1.0
- horizon: 2027-01-01
- lag: revenue >= 1000 per month
- lead: conversation >= 1 per week
- tripwire: signed-client = 0 by 2026-06-30 -> Revisit the offer

**Diagnosis.** One sentence on the obstacle.
**Guiding policy.** How, and what is not being done.
**Path.** 1. First precondition — assumes X (untested). 2. Second …
**Derisks.** It failed because … (catch early: …).

### example-area
- kind: area
- weight: 0.8
- standard: one sentence the model applies
- below-the-line: overdue; stale > 30 days

**Standard.** …
**Not doing.** …
```

`CLAUDE.md` — add a `## Strategy layer` section after `## Recurring tasks`:

```markdown
## Strategy layer

A `## Strategy` section of the private standing context (authoring guide:
the context repo's `docs/strategy.md`) declares outcome goals and areas.
`services/strategy.py` parses it; enrichment and gate 2 read it as a cached
system block and return what a task `serves` and in what `role`
(`path|derisk|support`), written to the task as `serves:<id>` / `role:<r>`
tags once (same claim guard as story points). `services/prioritize.py`
scores a `necessity` term behind `[necessity].mode` in `config/prioritize.toml`
— `flag` (ships; ranking unchanged), `demote`, `suppress` (adds the
`stop_doing` bucket and medium-confidence gate-2 suppression). A
high-confidence "serves nothing" at gate 2 never becomes a task in any mode;
`POST /suppressions/{message_id}/restore` reverses one. The daily tick
evaluates lead measures, lag reports, tripwires (fired → one `[P1]` task
with `external.gid = tripwire:{goal}:{n}:{by}`) and below-the-line signals
(debounced over 3 days; evidence signals boost path/derisk tasks, absence
signals only report), and saves a `strategy_snapshot` so tasks-api and the
webhook CF never mount the secret. `GET /review` and the Monday
`tasks-weekly-review` scheduler (comment on the `review:weekly` task) are
the read side; `GET /calibrate` reports necessity agreement, which is what
justifies moving `mode`. Design:
`docs/superpowers/specs/2026-10-09-strategy-layer-design.md`.
```

Also add `goal_state`, `goal_reports`, `goal_overrides`, `strategy_snapshot` to the Database row of the Stack table, and `tasks-weekly-review` to the Scheduler rows.

- [ ] **Step 5: Dry-run script**

```python
#!/usr/bin/env python3
# scripts/test-review.py
"""Render the weekly review from the live database without posting.

  (set -a; source .env; set +a; .venv/bin/python scripts/test-review.py --dry-run)

Without --dry-run it posts the comment on the standing task, exactly as the
Monday scheduler does."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.db import get_conn
from handlers import weekly_review
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import review
from services.due_digest import today_local


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.dry_run:
        print(weekly_review.run())
        return 0
    today = today_local()
    with get_conn() as conn:
        strategy = repo_goals.load_snapshot(conn)
        states = repo_goals.get_states(conn, today) or repo_goals.get_states(conn, today.fromordinal(today.toordinal() - 1))
        body = review.build(strategy, states, repo.list_scores(conn), repo_sup.list_necessity(conn), today)
    print(review.render(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run everything**

Run: `.venv/bin/pytest tests/ -q`
Expected: all pass. Then `.venv/bin/ruff check . && .venv/bin/ruff format --check .` (or whatever `scripts/` / CI uses — see `.github/workflows/*.yml`) clean.

- [ ] **Step 7: Commit**

```bash
git add .claude/agents/task-next.md .claude/agents/task-builder.md .claude/skills/prioritizing-tasks/SKILL.md scripts/task_next.py scripts/test-review.py context/standing-context.example.md CLAUDE.md tests/test_task_next.py
git commit -m "feat(strategy): task-next review/report/mute/restore, agent and skill rules, example strategy, docs"
```

---

## Rollout (after merge)

1. `scripts/migrate_db.py` (new tables and columns are all `IF NOT EXISTS`).
2. `/terraform-apply`: the mount, the IAM binding, the scheduler job. `/deploy-tasks` for both CFs; `deploy-api.yml` for tasks-api.
3. Write `strategy.md` in the context repo (its `docs/strategy.md` guide), merge, wait for the next cold start.
4. Trigger `day_changed` by hand (`gcloud pubsub topics publish task-events --message '{"kind":"day_changed"}'`) so the heal re-judges every task against the strategy and the snapshot lands; read `task-next review`; correct tags in Asana.
5. After a few weeks: `task-next calibrate` → `necessity.by_confidence`. Move `[necessity].mode` by config edit and redeploy.

## Self-review notes

- **Spec coverage.** D1 → Task 12 (mount) and the `strategy_snapshot` in Tasks 5/8/11 (the API never reads the secret). D2/D3 → Task 1. D4 → Task 3. D5 → Task 6. D6 → Tasks 2, 4. D7 → Tasks 7, 8. D8 → Task 8. D9 → Task 7 (plus mute in Task 11). D10 → Task 11 (`necessity_calibration`). D11 → Tasks 11, 12. D12 → Task 11. D13 → Tasks 1, 6, 8 (EMPTY path). D14 → Tasks 9, 10, 11 (restore). §Model calls → Task 3 (Opus 5.5, fallback, cached blocks), Task 9 (Sonnet 5 unchanged). Observability → Task 8. Testing section → every task's Step 1.
- **Deviation from the spec, deliberate:** the restore endpoint builds the task from the stored subject, sender, reason and link rather than re-running summary and deadline extraction, because the suppressed row holds no body and the API has no inbox credentials. It still goes through `create_task_from_fields`, carries `external.gid = message_id` so the pipeline's dedupe holds, and publishes `task_changed` so enrichment judges it afresh. Note this in the spec's D14 when the plan is accepted.
- **Type consistency checked:** `Serve` lives in `models/prioritize.py` (used by Tasks 3, 4, 6); `Strategy.EMPTY`, `Goal`, `GoalState` in `models/strategy.py`; `pz.ROLE_RANK` defined in Task 4 and used in Tasks 6 and 10's `necessity_outcome` (which inlines the same dict to avoid a handler→scorer import; keep them equal); `repo_goals.load_snapshot` returns a `Strategy` whose goals carry only id/kind/weight/horizon, which is all `review.build` and the router validations read.
- **Review Focus coverage:** 1 → Task 4 `test_unknown_goal_tag_is_unattached_and_groomed`; 2 → Task 8 `test_edited_by_date_is_a_new_tripwire`; 3 → Task 10 `test_no_strategy_ignores_the_judgment_entirely`; 4 → Task 7 `test_overdue_default_requires_p1_path_actionable_past_grace` (snoozed case); 5 → Task 6 `test_confident_serves_is_written_back_once`.

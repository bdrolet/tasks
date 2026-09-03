# Recurring tasks — a completion-anchored repeat rule carried in an Asana tag

**Date:** 2026-09-03
**Status:** designed, not implemented

## Problem

There is no way to say "this comes back". Every task in the system is
one-shot: the pipeline creates it from an email, or the API creates it from a
request, and completing it moves it to Done and ends the story
(`handlers/task_complete.py`).

The recurrence that is actually wanted is not calendar-anchored ("every
Monday") but **completion-anchored**: the next occurrence is due a fixed
interval *after the last one was finished*. Change the furnace filter, then
again three months after that — not three months after some notional schedule
date that has been drifting while the filter sat dirty.

### Why not Asana's native recurrence

Asana supports this in the product ("Repeat periodically — N days/weeks/months
after completion"). It is unusable here:

The Asana public API has **no recurrence field at all**. Grepping the
published OpenAPI spec
(`https://raw.githubusercontent.com/Asana/openapi/master/defs/asana_oas.yaml`,
3.1 MB) for `recurr` or `repeat` returns only two hits, both prose about
repeating a *search query*. There is no property to set it, no property to
read it, and no way to tell from the API that a task is recurring at all.

So native recurrence means clicking in the Asana UI for every recurring task,
and `task-builder`, `editing-tasks`, and every other consumer skill stay blind
to it. A recurrence feature that no agent in the system can create or see
does not serve the workflow this repo exists to support.

## Goals

- A task can carry a repeat rule that survives in Asana, not only in Postgres.
- Completing an occurrence creates the next one, due
  `completion date + interval`.
- The rule is settable and removable through the surface that already exists:
  the Asana UI by hand, and `add_tags` / `remove_tags` on the tasks-api.
- A recurrence failure never crashes a completion event or blocks the Done
  move.

## Non-goals

- **Calendar-anchored recurrence** ("every Monday", "the 1st of each month").
  Different feature, different semantics; not needed for the case that
  prompted this.
- **Subtask trees.** The next occurrence copies the task, not its subtasks.
- **Time-of-day.** v1 is date-only; `due_at` is not carried forward.
- **End conditions** (repeat N times, repeat until a date). Remove the tag.
- **Backfilling** recurrence onto tasks completed before this ships.

## Decisions

### D1 — Asana is the source of truth for the rule, via a tag

The rule is a tag on the task: `repeat:3mo`. Not a `recurring_tasks` table.

CLAUDE.md already states the principle this follows: *"Asana is the source of
truth; a DB outage degrades lookups to the `external:{message_id}` fallback
and must never crash an event."* A recurrence rule living only in Postgres
inverts that — lose the row and the chain silently ends, with nothing visible
in Asana to say it ever existed.

The tag also gets three properties for free:

- **Visible.** The chip shows on the task in the Asana UI.
- **Editable everywhere.** `add_tags` / `remove_tags` already exist on
  `POST /tasks` and `PATCH /tasks/{gid}`; no new endpoints.
- **Self-propagating.** The next occurrence copies tags, so it inherits the
  rule without anything having to remember it.

Cost: a handful of `repeat:*` entries in the workspace tag list, and a parser
that must fail safely.

### D2 — The next occurrence is a new task, created immediately

On completion, create a new Asana task dated `completed_at + interval`.

Rejected alternatives:

- **Lazy materialization** (record "next due 2026-12-03", let the daily
  escalation scheduler create it when the date arrives). This makes Postgres
  load-bearing for a task's existence: a broken sweep means tasks silently
  never appear — the worst failure mode for a system whose job is not dropping
  things. Immediate creation puts the durable state in Asana, and the DB
  matters only at the *next* completion.
- **Reviving the same task** (push `due_on`, un-complete). One gid forever,
  no content copying — but no completion history, and it fights the Done-section
  move in `handlers/task_complete.py`, visibly bouncing in and out.

A future-dated open task is not clutter here: listing queries are due-date
driven, so an occurrence due in three months does not surface until it is
near.

### D3 — `relativedelta` for the arithmetic

`datetime.timedelta` has days, seconds and weeks and deliberately no months or
years, because a month is not a fixed duration. Approximating (3 months ≈ 90
days) drifts off the calendar date across occurrences.

`dateutil.relativedelta` covers all four units in one type and handles
end-of-month clamping:

```python
date(2026, 1, 31) + relativedelta(months=1)   # date(2026, 2, 28)
date(2026, 9,  3) + relativedelta(days=10)    # date(2026, 9, 13)
```

This collapses what would otherwise be an `Interval(count, unit)` dataclass
plus a hand-rolled month-add into a parse and a `+`. `python-dateutil` is
added to `requirements.txt` and `requirements-dev.txt` — it is ~250 KB with no
transitive dependencies, and is already present in `.venv` as a transitive
dep, just undeclared. Ten lines of bespoke calendar arithmetic in a Cloud
Function is the riskier choice, not the leaner one.

There is no stdlib type that round-trips a duration *string*: `timedelta` has
no parser and no `__format__`. The parse is ours either way, so the tag uses
`3mo` rather than ISO 8601 `P3M` — the chip has to be readable in the Asana
tag picker.

### D4 — Idempotency is enforced Asana-side, not DB-side

The successor is created with `external.gid = "recur:<completed_gid>"`, and
`find_task_by_external` is checked first. A webhook redelivery, or an
un-complete followed by a re-complete, finds the existing successor and does
nothing.

This is deliberately not a `spawned_gid` column: the guard has to survive a DB
outage, since duplicate task creation is exactly the kind of visible mess a
best-effort DB write cannot be trusted to prevent.

### D5 — The tag marks the live occurrence

After the successor is created, the `repeat:` tag is **removed from the
completed task**. Exactly one open task in a series carries the tag, so
searching the tag returns the live occurrence rather than the whole history.

Ordering is create-then-strip. If the strip fails, the worst case is a
possible duplicate on a re-complete (already caught by D4); if the order were
reversed and creation failed, the chain would be dead with no record.

## Architecture

### `services/recurrence.py` (new)

Owns the grammar and the field-building. Calls `clients/asana.py`, in the
manner of `services/escalation.py`; no direct HTTP.

```python
TAG_PREFIX = "repeat:"

def parse(tag_name: str) -> relativedelta | None
def find_rule(tags: list[dict]) -> tuple[str, relativedelta] | None
def spawn_next(task: dict, detail: dict, section: dict | None) -> str | None
```

`spawn_next` owns the whole successor sequence and returns the new gid (or
`None` when the idempotency guard short-circuits): check
`find_task_by_external("recur:<gid>")`, create the task, strip the `repeat:`
tag from the completed one, post the forward-link comment, refresh the task
index, increment the counter.

**Grammar.** `repeat:<count><unit>`, case-insensitive, whitespace tolerated
around the count:

| Unit | Accepted | Maps to |
|---|---|---|
| days | `d`, `day`, `days` | `relativedelta(days=n)` |
| weeks | `w`, `week`, `weeks` | `relativedelta(weeks=n)` |
| months | `mo`, `mon`, `month`, `months` | `relativedelta(months=n)` |
| years | `y`, `yr`, `year`, `years` | `relativedelta(years=n)` |

Bare `m` is **rejected** — ambiguous between minutes and months, and guessing
wrong produces a task 30x early or late. Count must be an integer in 1..3650;
`0` and non-numeric counts are rejected.

`parse` never raises. An unparseable `repeat:` tag logs a warning and returns
`None`.

**Ambiguity rules.** `find_rule` returns `None` and logs a warning when a task
carries **more than one** parseable `repeat:` tag. Guessing which one was
meant is worse than doing nothing and letting the tags be corrected.

**Timezone.** `completed_at` comes back from Asana in UTC. The due date is
computed from that instant converted to `America/New_York` — completing at
8pm ET would otherwise date the successor a day late. This introduces the
repo's first `zoneinfo` use; it stays local to this module rather than
becoming a repo-wide convention. `America/New_York` matches the Cloud
Scheduler timezone already configured for `tasks-escalation`.

### `handlers/task_complete.py` (modified)

One guarded step, ordered so the section is read before the Done move
overwrites it:

1. *(existing)* `asana.get_task(gid)`; bail if this is an un-complete event.
2. **new** — `find_rule` on the tags now present in that response. No
   `repeat:` tag → the handler proceeds exactly as it does today.
3. **new** — on a match: capture `asana.current_section(task)`, fetch
   `asana.get_task_detail(gid)` for the description and assignee, and call
   `recurrence.spawn_next`. Wrapped in `try/except` — a failure here logs and
   falls through.
4. *(existing)* `repo_tasks.mark_completed`, `repo_index.set_completed`, move
   to Done.

### `clients/asana.py` (modified)

`get_task`'s `opt_fields` grows `tags.gid,tags.name,completed_at`, so the tag
check costs no additional API call on the common (non-recurring) completion.
`DETAIL_OPT_FIELDS` already carries everything the copy needs
(`html_notes`, `tags`, `assignee.gid`, `memberships.section.gid`, `due_on`).

### What the successor copies

| Carried | Not carried |
|---|---|
| `name` (so the `[PX]` prefix rides along) | comments / stories |
| `html_notes` | subtasks |
| project + the section the completed task was in | attachments |
| all tags, including `repeat:` | `due_at` time-of-day |
| `assignee` | `completed`, `completed_at` |
| `due_on` = local completion date + interval | the original `external.gid` |

Section placement: the successor lands in the section the completed task
occupied immediately before the Done move. If that was already Done, or the
task was unsectioned, the successor is created unsectioned — `for_category`'s
Review default is a mail-routing decision and does not apply to a chore.

`external.gid` is replaced by `recur:<completed_gid>` (D4). For a recurring
task that originated from an email, this means the successor is no longer
reachable via `external:{message_id}` — correct, since the successor is not
that email's task.

### Recording

`services/task_index.refresh(new_gid)` is called explicitly after creation.
Asana also fires an `added` webhook event that refreshes it; the call is
idempotent and removes the dependency on that delivery arriving.

No row is written to `tasks` — that table is keyed by a `UNIQUE NOT NULL`
`message_id` and is specifically the email-derived-task ledger. A successor
has no message.

A comment is posted on the **completed** task linking forward to its
successor, so a finished occurrence carries a visible trail to the next one.

### Observability

New counter `asana.recurrences`, declared alongside `asana.escalations` in
`clients/otel.py` and incremented on successful creation. Per
`.claude/skills/adding-observability`.

## API surface

No new endpoints. `add_tags: ["repeat:3mo"]` on the existing `POST /tasks` and
`PATCH /tasks/{gid}` is the whole interface, and `services/tags.py::resolve_gids`
creates the tag on first use.

One addition: `api/routers/tasks.py` **validates** tags matching `repeat:*`
against `recurrence.parse` and returns 400 with the accepted grammar on a
failure. Without this, `repeat:3months`-style typos silently create a real,
dead tag that looks live in the UI — the single most likely way this feature
quietly does nothing.

Consumer documentation: the `creating-tasks` and `editing-tasks` skills and
the `task-builder` agent get a line describing the tag.

## Testing

- **Parse table** — every accepted unit and alias; rejections for bare `m`,
  `0`, negative, non-numeric, empty unit, unknown unit.
- **Month-end clamping** — Jan 31 + 1mo → Feb 28; Feb 29 in a leap year.
- **Timezone** — a `completed_at` of 2026-09-03T23:30:00Z (7:30pm ET) with
  `repeat:1d` yields 2026-09-04, not 2026-09-05.
- **Field copying** — name, notes, tags, assignee, project and section
  carried; comments, subtasks and `due_at` not.
- **Idempotency** — an existing `recur:<gid>` external task short-circuits
  creation.
- **Ambiguity** — two `repeat:` tags creates nothing and logs; an unparseable
  tag creates nothing and logs.
- **Failure isolation** — `spawn_next` raising still leaves the task marked
  completed in the DB and moved to Done.
- **No-op path** — a completion with no `repeat:` tag makes no additional
  Asana calls.

## Files

| File | Change |
|---|---|
| `services/recurrence.py` | new — grammar, rule lookup, successor creation |
| `handlers/task_complete.py` | the guarded recurrence step |
| `clients/asana.py` | `get_task` opt_fields += `tags.gid,tags.name,completed_at` |
| `clients/otel.py` | `asana.recurrences` counter |
| `api/routers/tasks.py` | `repeat:*` tag validation |
| `requirements.txt`, `requirements-dev.txt` | `python-dateutil` |
| `tests/test_recurrence.py` | new |
| `tests/test_task_complete.py` | recurrence cases |
| `CLAUDE.md` | a Recurring tasks section |
| `.claude/skills/{creating,editing}-tasks`, `.claude/agents/task-builder.md` | the tag |

No DB migration. No Terraform change. Ships through the normal
`deploy-tasks` path.

## Risks

- **Un-completing after the successor exists is manual cleanup.** Delete the
  successor, re-add the `repeat:` tag. The alternative — delaying creation
  until some grace period passes — reintroduces the lazy-materialization
  failure mode rejected in D2.
- **A webhook redelivery between create and tag-strip can double-create.** The
  D4 external-gid guard closes this: the retry finds `recur:<gid>` and stops.
  The residual window is a redelivery arriving before the first creation's
  `POST /tasks` returns, which Asana's retry timing makes very unlikely.
- **Tag-list growth.** Every distinct interval adds a workspace tag. Bounded
  in practice by how many distinct intervals get used.
- **Silent chain death** if the `repeat:` tag is removed by accident — for
  instance by a `remove_tags` call that was aiming at something else. Nothing
  detects this; the task simply stops coming back. Accepted for v1.

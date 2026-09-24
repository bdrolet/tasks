# Task start dates — scheduling work backward from the deadline

Status: designed (v2, 2026-09-24 — rewritten after the prioritizer shipped)
Date: 2026-09-09 (v1), 2026-09-24 (v2)

## What changed since v1

v1 was written before the service had any notion of effort. It asked a
Sonnet call to *guess* lead time ("gathering three years of returns takes a
few days, so start the 17th") and to write `start_on` once, at creation,
never to be recomputed. Three of its premises are now false:

- **Effort exists.** Every task carries a story-point estimate (`Story
  points` custom field; `docs/superpowers/specs/2026-09-23-next-prioritizer-design.md`
  D1, D5), and `config/prioritize.toml` states the daily capacity. Lead time
  is arithmetic, not judgment.
- **The scorer already computes when work must begin.** The feasibility pass
  produces `simulated_start` and `effective_slack` for every hard-dated task
  on every rescore. A stored, model-guessed `start_on` would duplicate that
  and drift from it the moment an estimate or a due date changed.
- **Actual starts are recorded.** The `Started at` custom field is the day
  work really began. v1 had no such thing, so "a start date that has passed
  is a fact, not a trigger" was the only honest stance. Now the gap between
  planned and actual start is a signal.

The `due_at` half of v1 was right and is kept as written. The `start_on`
half is replaced: **`start_on` is derived by the prioritizer from the due
date and the points, written back to Asana with an ownership guard, and a
passed planned start with no actual start is a late-start signal.** No new
model call.

## Problem

A task carries `due_on`, the day it must be finished. That answers "what is
late" and nothing else.

**No time of day.** The pharmacy refill tasks of 6–8 Sep all carried
`due_on: 2026-09-09` while their actual cutoff — *"confirm by 11:00am"* —
lived only in the description prose. `services/deadline.py` returns a bare
`YYYY-MM-DD`; `clients/asana.py::create_task` only ever sets `due_on`.

**No visible start.** The prioritizer knows a 5-point task due in ten days
must begin in eight, but that knowledge lives in a `components` JSON blob.
Asana's Timeline view (Starter) can draw it; the due-day digest could say
"starts today"; neither can, because `start_on` is never written.

**No late-start signal.** When a planned start passes and `Started at` is
still empty, the task is quietly becoming a crisis. Today the only pressure
is the deferral counter, which needs the task to have been *offered* first.

## Goals

- Set `due_at` when a source states a real clock time (unchanged from v1).
- Write `start_on = due − lead` for every hard-dated, pointed task, and keep
  it true as points and due dates change.
- Never overwrite a start date a person set.
- Surface starts where tasks are already consumed: Asana Timeline (for
  free, once the field is written), the due-day digest, `POST /search`,
  and `--explain`.
- Treat a passed planned start with no actual start as a stale signal.

## Non-goals

- **Escalation stays due-only.** A missed start does not move a task to
  Overdue; `escalated_at` keeps its one-shot semantics.
- **No `start_at`.** v1 allowed callers to write a start *time*; nothing
  ever produced or consumed one. Dropped from the write surface. `due_at`
  remains.
- **No new calendar events for starts.** Starts fold into the existing
  due-day event (D5).
- **No start for soft-dated tasks.** A horizon or inferred due date is a
  scoring device (prioritizer D13); writing a start date derived from it
  would put an invented commitment on the Timeline.

## Decisions

### D1 — Due may be timed; start never is *(unchanged)*

`due_at` is written only when the source states a clock time. No default
hour is invented. Start dates are always `start_on`.

Asana permits the mix (`start_on` + `due_at` is legal; `start_on` requires a
due field in the same request; `due_on`/`due_at` are mutually exclusive on
write).

### D2 — `start_on` is derived, not inferred

For a task with a hard `due_on` (or `due_at`) and a points value:

```
lead_days = ceil(effort_days)           # effort_days = components["effort_days"]
start_on  = due_on − lead_days
```

`effort_days` is the scorer's own figure — effective points (field >
override > estimate > default) ÷ `capacity.points_per_day`, **including the
`low_confidence_multiplier`** for unconfirmed estimates — so the planned
start and the daily selection cannot disagree about how long a task takes.
Computed inside `services/prioritize.score_set` as
`components["planned_start"]`; pure, no clamp to today (that belongs to the
writer, D3).

No model call. `services/deadline.py` keeps its v1 widening to
`extract_schedule` **only for `due_at`** — the start half of v1's D3/D4 is
gone.

### D3 — The prioritizer writes `start_on`, with an ownership guard

After each rescore, `handlers/prioritize.py` compares `planned_start` with
the task's stored `start_on` and writes when they differ, subject to:

`task_facts` gains `start_on_written DATE` — the value this service last
wrote, `NULL` meaning never (the same shape as `points_estimated`: written
only by the guard below, never by `upsert_facts`, and never by the gather).
"Ours" is `start_on IS NOT DISTINCT FROM start_on_written`.

| Stored `start_on` | vs `start_on_written` | Action |
|---|---|---|
| empty | any | write `max(planned_start, today)`, record it |
| equal to `start_on_written` | ours | write the new `planned_start`, record it |
| anything else | not ours — a person set it | **leave it** |
| ours | task lost its hard due date or points | clear `start_on`, set `start_on_written = NULL` |

The write is claimed with a conditional update, mirroring `claim_estimate`:
`UPDATE task_facts SET start_on_written = %s WHERE task_gid = %s AND
start_on_written IS NOT DISTINCT FROM %s` (the previous value) — whoever
wins the row writes to Asana. A start date typed in Asana therefore sticks
until the person clears it; a start date we wrote tracks the estimate.

`sync_start_dates` runs **after** the rescore transaction has committed and
outside `lock_rescore`, the same A/B split `handle_task_changed` already
uses for the points write-back: the Asana `PUT` never holds the scoring
lock. Writes are idempotent and bounded: one `PUT` per task per change of
`planned_start`, each including the due field Asana requires; the webhook
echo gathers, finds the content hash unchanged, and only rescores.
Failures log and retry on the next rescore.

**Side effect on aging.** Every `start_on` write bumps the task's Asana
`modified_at`, which is `days_stale` → the `A` term and the `aged` stale
reason. The first rollout writes a start on every hard-dated pointed task
at once, resetting their aging together; after that a write happens only
when an estimate or due date changes, which is itself activity.

### D4 — A passed planned start with no actual start is stale

`stale_reason = "late_start"` when `start_on < today`, `Started at` is
empty, and the task is not completed. It is evaluated **last** in the stale
precedence (`aged`, then `deferred`, then `soft_due_passed`, then
`late_start`) and joins them in the stale side list and in `--explain`. It does not
change the score: the scorer's urgency already reflects slack, and a second
push would double-count. It is a *label* for the person.

### D5 — Starts join the day's existing digest event *(unchanged)*

One all-day event per (day, calendar). Its body gains a "Starting" group
below the due tasks; the title becomes `"2 due · 1 starting"` (`2 tasks
due` when nothing starts, so existing events keep their content hash). The
title regex `handlers/due_digest.py::_DIGEST_TITLE_RE`, which `_adopt` uses
to reclaim an orphaned event, is widened to
`^(?:\d+ tasks? due|\d+ due(?: · \d+ starting)?|\d+ starting)$` so events
created before the change still adopt. A task can contribute to two days —
its start day and its due day.

### D6 — Recurrence recomputes; it does not carry the lead

v1 copied the lead offset onto the successor. Under D2 the successor's
`start_on` is simply recomputed on its first rescore from its own due date
and its copied points, so `services/recurrence.py` sets **no** start field.
One rule, one place.

### D7 — `Started at` and `start_on` are different facts

`start_on` is when work *must* begin; `Started at` is when it *did*. The
difference, aggregated in `GET /calibrate` as `mean_start_lag_days` per
project, is the calibration signal for whether the point scale or the
capacity is off — a systematic late start means the plan is too optimistic
before any task is late.

## Architecture

### `services/prioritize.py`
- `score_set`: `components["planned_start"]` (ISO date or `None`) for
  hard-dated tasks with points; `components["late_start"]` boolean; stale
  reason `late_start` per D4.

### `handlers/prioritize.py`
- After `rescore`, `sync_start_dates(conn, scored, facts)` applies D3 for
  tasks whose `planned_start` differs from stored `start_on`; one
  `asana.update_task(gid, {"start_on": ...})` per change (with the due field
  echoed, as Asana requires). Metric `asana.prioritize.start_writes{result=written|cleared|kept_manual|failed}`.

### `repo/prioritize.py` / `repo/schema.sql`
- `task_facts.start_on_set_by_us`; `claim_start(conn, gid, start_on) ->
  bool` and `release_start(conn, gid)`, mirroring `claim_estimate`.

### `services/deadline.py`, `handlers/task_create.py`, `clients/asana.py::create_task`
- `extract_schedule(event) -> Schedule(due_on, due_at)` replaces
  `extract_deadline`; same Sonnet call, same P0/P1 gate, same 3000-char
  window. Normalized in code, never trusted to the model: unparseable →
  empty; both `due_on` and `due_at` → keep `due_at`; malformed → that field
  `None`. `create_task` writes the due field that is set. **No `start_on`
  at creation** — the first rescore writes it.

### `services/due_digest.py`, `handlers/due_digest.py`, `models/digest.py`
- `in_window` becomes `days_in_window(task, today, days=30) -> list[(day,
  kind)]`, `kind ∈ {due, start}`; `DigestTask.due_on` is renamed `day` and
  joined by `kind` (grouping on a field called `due_on` that sometimes holds
  a start date is a quiet lie); `build_events` splits each day into a due
  run then a starting run; `title_for(due, start)`; the widened title regex
  (D5); bullets condensed once per gid, before the pairs are expanded.

### `api/routers/tasks.py`, `api/routers/search.py`, `services/task_search.py`
- `TaskDetail.start_on` already exists. `CreateTaskRequest`/`UpdateTaskRequest`
  gain `start_on` (nullable; explicit null clears). A person's write needs
  no bookkeeping: the next gather stores the new `start_on`, which no
  longer equals `start_on_written`, so the guard reads it as not ours.
  Validation: `start_on` without any due field → 400; `due_on` and `due_at`
  both set → 400. No `start_at`.
- `SearchRequest`: `start_before`, `start_after`, `startable` (sugar for
  `start_before = today`); `SearchResult.start_on`, `due_at`.
- `GET /calibrate`: `mean_start_lag_days` per project (D7).

### `.claude/skills/*`, `task-next`
- `--explain` shows `planned_start` and `late_start`; the stale block shows
  `stale:late_start`. `searching-tasks` documents `startable`. `task-lister`
  learns "what can I start now".

## Observability

`asana.prioritize.start_writes{result}` (above). `tasks_created` gains
`dated` (`none`/`due_on`/`due_at`) as in v1; `has_start` is dropped (starts
are no longer created at creation time).

## Testing

Unit, no network:

- `planned_start` arithmetic: 1p/5p/8p against capacity 5; ceil at
  boundaries; no start for soft-dated or unpointed tasks; never before today
  on first write.
- D3 table: each row, including "manual value untouched" and "we clear what
  we wrote when the due date is removed"; the claim is conditional in SQL.
- D4: `late_start` fires only with a past `start_on`, empty `Started at`,
  not completed.
- Digest: as v1 (`days_in_window`, `title_for`, regex).
- API validation: the two 400 cases; PATCH evaluated against merged state.
- `filter_tasks` start bounds and `startable`.
- Recurrence: successor carries no start field.

End-to-end: create a hard-dated 3-point task via `POST /tasks`; after the
event rescore, `GET /tasks/{gid}` shows `start_on = due − 1`; change points
to 8 → `start_on = due − 2`; type a different `start_on` in Asana → it
sticks across a rescore; `scripts/test-digest.py --dry-run` shows a day
with both due and starting tasks.

## Files

| File | Change |
|---|---|
| `services/prioritize.py` | `planned_start`, `late_start`, stale reason |
| `handlers/prioritize.py` | `sync_start_dates` after rescore |
| `repo/prioritize.py`, `repo/schema.sql` | `start_on_written`, `claim_start`, `release_start` |
| `services/deadline.py` | `extract_schedule` (due half only) |
| `clients/asana.py` | `create_task` writes `due_at`; opt_fields already carry `start_on` |
| `handlers/task_create.py` | schedule local (due only) |
| `services/due_digest.py`, `handlers/due_digest.py`, `models/digest.py` | as v1 |
| `api/routers/tasks.py`, `api/routers/search.py`, `services/task_search.py`, `api/routers/next.py` | `start_on` write + validation; start filters; `mean_start_lag_days` |
| `clients/otel.py` | `prioritize.start_writes` |
| `.claude/skills/*`, `.claude/agents/task-next.md` | document `startable`, `planned_start`, `late_start` |

One schema migration (a column with a default — `migrate_db.py` re-runs the
file; add `ALTER TABLE task_facts ADD COLUMN IF NOT EXISTS ...`). No
Terraform change. No new secret.

## Risks

**Write churn.** Every points edit on a hard-dated task moves `start_on`
one write. Bounded to one `PUT` per real change, and the webhook echo does
not re-enrich (hash unchanged). Acceptable; measured by `start_writes`.

**A start date typed in Asana that happens to equal ours.** The guard reads
"equals the last value we wrote" as ours, so a person who types the same
date we computed will see it move when the estimate changes. The
alternative — never touching a field a person has ever edited — needs
Asana's story history and is not worth it for a coincidence.

**Two digest entries for one task** — as v1: intended, disambiguated by the
section headers.

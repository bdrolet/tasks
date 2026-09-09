# Task start dates — scheduling work backward from the deadline

Status: designed
Date: 2026-09-09

## Problem

A task today carries one date: `due_on`, the day it must be finished. That
answers "what is late" and nothing else. Two gaps follow.

**No time of day.** The pharmacy refill tasks of 6–8 Sep all carried
`due_on: 2026-09-09` while their actual cutoff — *"confirm by 11:00am"* —
lived only in the description prose. A task due at 11am and a task due at
11pm are indistinguishable to every consumer in the service. Asana has
carried `due_at` since forever; `services/deadline.py` has only ever
returned a bare `YYYY-MM-DD`, and `clients/asana.py::create_task` only ever
sets `due_on`.

**No sense of when to begin.** A task due in three weeks that needs three
days of document-gathering is invisible until it is nearly late. The
due-day digest shows it on one day — the last one. There is no answer to
"what should I be working on today" that is distinct from "what is due
today."

Asana models both. `start_on`/`start_at` express the day work begins, and
appear nowhere in this repo.

## Goals

- Set `due_at` when a source states a real clock time, so the 11am cutoff
  lands on the task instead of in its prose.
- Set `start_on` when a task has a real deadline and implied lead time, so
  work surfaces when it should begin rather than when it is due.
- Surface start dates in the two places tasks are already consumed: the
  due-day digest and `POST /search`.
- Keep all four fields readable and writable through tasks-api.

## Non-goals

- **Escalation stays due-only.** A missed start date does not escalate. The
  Overdue section keeps meaning "past due", and `escalated_at` keeps its
  one-shot semantics. Revisit only if missed starts turn out to predict
  overdue tasks.
- **No effort estimates, no duration field.** Start and due bound a range;
  the service takes no position on how many hours sit inside it.
- **No rescheduling.** Nothing recomputes a start date after creation. A
  start date that has passed is a fact, not a trigger.
- **No new calendar events for starts.** Starts fold into the existing
  due-day event (D5), not a second event series.

## Decisions

### D1 — Due may be timed; pipeline-set start never is

`due_at` is written only when the source states a clock time. Everything
else stays `due_on`. No default hour is invented — an end-of-day default
would make every task a timed event and assert precision the email never
gave.

Start dates written by the pipeline are always `start_on`. "Begin working
on this Monday" has no meaningful clock time, and inventing one would put
start entries on the calendar as timed events.

Asana permits the mix. Per the `createTask` schema:

> `start_on`: *"Note: `due_on` or `due_at` must be present in the request
> when setting or unsetting the `start_on` parameter."*
>
> `start_at`: *"Note: `due_at` must be present in the request when setting
> or unsetting the `start_at` parameter."*

So `start_on` + `due_at` is legal. `due_on` and `due_at` are mutually
exclusive on write, as are `start_on` and `start_at`.

### D2 — `start_at` is writable by callers, never by the pipeline

tasks-api accepts `start_at` and passes it through, rejecting a request
that sets it without `due_at` (400, before any Asana I/O — a clearer error
than Asana's, and the same status the router already returns for an invalid
priority or a malformed `repeat:` tag). Claude inference only ever produces
`start_on`.

This keeps all four fields writable without any code path inventing a start
time. A caller who genuinely means "start this at 9am" can say so; nothing
guesses it.

### D3 — No due date means no start date

Start inference runs only where deadline extraction found an explicit
deadline. This is enforced structurally, not by prompt: `extract_schedule`
discards `start_on` when the response carries no due value. It matches the
existing rule that a task gets a date only when a real external deadline
exists, and it satisfies Asana's constraint for free — the service can
never emit a `start_on` with no due field beside it.

### D4 — One scheduling call, not two

`services/deadline.py::extract_deadline` becomes `extract_schedule` and
returns due and start together from a single Sonnet call.

The reasoning is joint: *"due the 20th, gathering three years of returns
takes a few days, so start the 17th."* A second call would re-read the same
email to re-derive the same facts at double the cost. It also puts the
P0/P1 gate, the exclusivity normalization and the clamp in one place.

The file keeps its name and its "called only for P0/P1" contract; only its
return type widens.

### D5 — Starts join the day's existing event, in their own section

One all-day event per (day, calendar) is unchanged. Its body gains a
"Starting" group below the due tasks, and the title becomes
`"2 due · 1 starting"`.

The alternative — a second event series for starts — needs a discriminator
column in `due_day_events`, whose primary key is `(day, calendar_id)`. That
is a schema migration and a doubling of calendar events to buy a visual
separation that a section already provides.

**Consequence:** `handlers/due_digest.py::_DIGEST_TITLE_RE`
(`^\d+ tasks? due$`) is what `_adopt` uses to reclaim an orphaned event
when its DB row is lost. A new title format that the old regex does not
match would orphan every event created before this change. The regex must
accept both forms — see D6.

### D6 — The title regex accepts old and new formats

```python
_DIGEST_TITLE_RE = re.compile(r"^(?:\d+ tasks? due|\d+ due(?: · \d+ starting)?|\d+ starting)$")
```

Adoption is a recovery path, not a hot path; a regex that matches one
retired format costs nothing and prevents duplicate events on any day whose
row was lost before the deploy.

### D7 — The digest window admits a task on either date

`in_window` currently requires `due_on` inside `[today, today + 30]`. A
task that starts inside the window and is due outside it must appear on its
start day; a task due inside the window keeps appearing on its due day.

A task can therefore contribute to two days' events — its start day and its
due day — which is the intent. `build_events` groups by
`(day, calendar_id)` already; the change is that a task now yields up to two
`(day, kind)` entries rather than exactly one.

### D8 — Recurrence carries the lead forward, and re-infers nothing

If the finished occurrence had `start_on` three days before its `due_on`,
the successor gets `start_on` three days before its new `due_on`. The
offset is computed in whole days from the two dates on the completed task.

Recurrence stays fully deterministic — no Claude call ever runs on a
successor. Consistent with the existing rule that recurrence copies neither
time-of-day nor comments, the successor never carries `due_at` or
`start_at`; a timed occurrence yields a date-only successor, and the lead is
computed from the derived `due_on` Asana returns.

If either date is missing on the completed task, the successor gets no
start date.

## Architecture

### `models/schedule.py` (new)

```python
class Schedule(NamedTuple):
    due_on: str | None    # YYYY-MM-DD
    due_at: str | None    # ISO 8601 UTC
    start_on: str | None  # YYYY-MM-DD


EMPTY = Schedule(None, None, None)
```

Pure type, no imports from other layers. `EMPTY` is a module constant, not a
class attribute: `typing.NamedTuple` treats every annotation in the class
body as a field, and a `ClassVar` annotation there raises `TypeError` at
class-creation time.

### `services/deadline.py` (modified)

`extract_schedule(event) -> Schedule` replaces `extract_deadline`.

The prompt keeps its `Calendar` standing-context preamble and its 3000-char
body window (matching `services/email_summary.py`, so the schedule pass and
the summary pass see identical context). It asks for a JSON object rather
than a bare date:

```json
{"due_on": "2026-09-20", "due_at": null, "start_on": "2026-09-17"}
```

Normalization, in code, after parsing — none of it trusted to the model:

1. Unparseable response, or any exception → `schedule.EMPTY`.
2. Both `due_on` and `due_at` present → keep `due_at`, drop `due_on`
   (Asana derives the date from the timestamp anyway).
3. Malformed date or timestamp → that field becomes `None`.
4. No due value → `start_on` is dropped (D3).
5. `start_on` outside `[today, due date]` → dropped, not corrected. A start
   date after its own due date is a model error; a wrong date is worse than
   none.

Fail-open throughout: every failure path yields a task with fewer dates,
never a crashed event.

### `clients/asana.py` (modified)

`create_task` takes `schedule: Schedule` in place of `due_date: str | None`.
It writes only the fields that are set, and writes them as a unit — Asana
rejects `start_on` in a request carrying no due field, so an internally
inconsistent `Schedule` yields no date fields at all rather than a 400.

`DETAIL_OPT_FIELDS` gains `start_on,start_at`.
`SEARCH_OPT_FIELDS` gains `due_at,start_on`.
`DIGEST_OPT_FIELDS` gains `start_on`.

`get_incomplete_tasks_past_due` is untouched — its `opt_fields` and its
`due_on < today` comparison stay as they are. Asana returns a derived
`due_on` for timed tasks, so a `due_at` task still escalates on the right
day. Escalation ignores start dates entirely (non-goals).

### `handlers/task_create.py` (modified)

The `due_date` local becomes `schedule`, defaulting to `schedule.EMPTY`:

```python
schedule = models.schedule.EMPTY
if verdict.priority in ("P0", "P1"):
    try:
        schedule = deadline.extract_schedule(event)
    except Exception:
        logger.exception("Schedule extraction failed for message_id=%s", event["message_id"])
```

Unchanged in shape from today's deadline block — same gate, same fail-open,
same one call.

### `services/due_digest.py` (modified)

`in_window` becomes `days_in_window(task, today, days=30) -> list[tuple[str, str]]`,
returning the `(day, kind)` pairs a task contributes, `kind` in
`{"due", "start"}`. Empty list for a completed or undated task.

`DigestTask.due_on` is **renamed to `day`** and joined by `kind: str`. The
rename is not cosmetic: `build_events` currently groups on `task.due_on`,
and a start entry's day is its start date, not its due date. Leaving the
field called `due_on` while it sometimes holds a start date is exactly the
kind of quiet lie that survives review and fails in production.

`build_events` then groups by `(day, calendar_id)` as now, splits each group
by `kind` into two ordered runs — due first, starting second — and emits one
`sections` list with a group header before each non-empty run. `order`
sorts within a run, not across, so a P0 start never jumps above a P3 due.

`title_for(due_count, start_count) -> str`:

| due | start | title |
|---|---|---|
| 2 | 0 | `2 tasks due` |
| 2 | 1 | `2 due · 1 starting` |
| 0 | 1 | `1 starting` |

The pure due-only case keeps today's exact wording, so the overwhelming
majority of existing events do not churn their content hash on deploy.

`content_hash` is unchanged in mechanism — it hashes title plus sections, so
the new grouping participates automatically.

### `handlers/due_digest.py` (modified)

`_digest_tasks` iterates `days_in_window`'s pairs instead of testing a
single membership, emitting one `DigestTask` per pair.

Bullet condensing runs **once per gid**, before the pairs are expanded, and
both entries share the resulting `points` list. Relying on the
`task_bullets` cache to absorb the second call would work only if the first
`put` were visible to the second `get` on the same connection mid-rebuild —
true today, but a silent dependency on transaction visibility that would
double the model spend the moment it stopped holding. Condensing once and
reusing makes the ≤40-calls-per-rebuild budget count tasks rather than
entries by construction, not by luck.

### `services/recurrence.py` (modified)

`spawn_next` computes the lead:

```python
def lead_days(detail: dict) -> int | None:
    """Whole days between start_on and due_on on the completed task."""
```

`None` when either is absent. When set, the successor's fields carry
`start_on = next_due - lead_days`. Never `start_at` (D8).

### `api/routers/tasks.py` (modified)

- `TaskDetail` gains `start_on`, `start_at`.
- `SubtaskSummary` gains `start_on`.
- `CreateTaskRequest` and `UpdateTaskRequest` gain `start_on`, `start_at`;
  both are nullable, and on PATCH an explicit null clears, joining the
  existing `for field in ("due_on", "due_at", "assignee")` loop.
- New validation, before any Asana I/O:
  - `start_at` without `due_at` in the same request → 400 with the Asana
    rule quoted.
  - `start_on` without any due field in the same request → 400.
  - `due_on` and `due_at` both set → 400.
  - `start_on` and `start_at` both set → 400.

On PATCH these are evaluated against the merged result of the request and
the task's current state, not the request alone — clearing `due_at` on a
task that has `start_at` is an error, and setting `start_on` on a task that
already has a due date is fine.

### `api/routers/search.py` and `services/task_search.py` (modified)

`SearchRequest` gains:

- `start_before: str | None` — inclusive `YYYY-MM-DD`
- `start_after: str | None` — inclusive
- `startable: bool = False` — sugar for `start_before = today`, the
  "what can I work on now" query

`SearchResult` gains `due_at`, `start_on`.

`filter_tasks` grows the same shape of bound test it applies to due dates,
against `start_on`. Start filters drop tasks with no start date, matching
how due filters drop undated tasks today. Sort order is unchanged — due
date ascending, undated last, then name.

## Consumer skills

`searching-tasks`, `fetching-task`, `editing-tasks` and `creating-tasks`
document the new fields; `task-lister` learns `startable` for "what should I
be working on". The listing format gains a start date only where one is
set — a task with no start reads exactly as it does today.

`scripts/task_ref.py` is untouched; refs stay hashed from the GID.

## Observability

`tasks_created` gains attributes `dated` (`none`/`due_on`/`due_at`) and
`has_start` (`true`/`false`), so the share of tasks receiving each kind of
date is visible without a new metric.

A new counter `asana_schedule_extractions` with a `result` attribute
(`ok` / `no_due` / `clamped` / `parse_error` / `exception`) makes the
normalization rules in D4 measurable — particularly how often the model
proposes a start date the clamp rejects, which is the signal for whether the
prompt needs work.

## Testing

Unit, no network:

- `Schedule` normalization — every rule in D4, each in its own test:
  both due fields set, malformed date, start with no due, start after due,
  start before today, unparseable JSON, exception from the client.
- `days_in_window` — due-only in window, start-only in window, both in
  window (two pairs), both outside, completed, undated, exact boundaries at
  day 0 and day 30 for each of start and due.
- `title_for` — the three rows of the D5 table, plus the singular/plural
  boundary at 1.
- `_DIGEST_TITLE_RE` — matches both retired and current title formats,
  rejects a user-authored event title (D6).
- `lead_days` — normal lead, zero lead, missing start, missing due, and a
  completed occurrence with `due_at` (successor gets date-only, lead
  computed from the derived `due_on`).
- API validation — each of the four 400 cases, and the PATCH-against-merged-
  state cases from the API section.
- `filter_tasks` — start bounds inclusive, undated dropped, `startable`.

End-to-end, against real Asana, via `scripts/test-task-create.py` and
`scripts/test-api-local.py --write`: a task created with `due_at` +
`start_on` round-trips all four fields through `GET /tasks/{gid}`.

`scripts/test-digest.py --dry-run` shows a day with both due and starting
tasks before anything writes to a calendar.

## Files

| File | Change |
|---|---|
| `models/schedule.py` | new — `Schedule` |
| `services/deadline.py` | `extract_schedule` replaces `extract_deadline` |
| `clients/asana.py` | `create_task(schedule=…)`; three opt_fields constants |
| `handlers/task_create.py` | schedule local, same P0/P1 gate |
| `services/due_digest.py` | `days_in_window`, `kind`, grouped sections, `title_for` |
| `handlers/due_digest.py` | one `DigestTask` per pair; title regex |
| `models/digest.py` | `DigestTask.due_on` → `day`; new `kind` |
| `services/recurrence.py` | `lead_days`, successor `start_on` |
| `api/routers/tasks.py` | four fields, four validation rules |
| `api/routers/search.py` | start filters, `startable`, new result fields |
| `services/task_search.py` | start bound filtering |
| `clients/otel.py` | `schedule_extractions` counter |
| `.claude/skills/*` | document the new fields |
| `docs/task-content-standard.md` | note that a stated cutoff belongs in `due_at`, not only prose |

No database migration. No Terraform change. No new secret or env var.

## Risks

**The model proposes bad start dates.** Mitigated by the clamp (D4 rule 5)
and measured by the `clamped` result attribute. A start date is dropped, not
corrected — the failure mode is a task with no start date, which is exactly
today's behaviour.

**Digest churn on deploy.** Every day whose event gains a starting task
re-renders once. Days with only due tasks keep their exact title and
sections, so their content hash is stable and they are not touched. Bounded,
one-time, and visible in `digest_events{op="update"}`.

**Two entries for one task read as duplication.** A task that starts on the
3rd and is due on the 20th appears on both days. This is the intent, but it
is the change most likely to read as a bug on the calendar. The section
headers ("Due" / "Starting") are what disambiguate; if they prove
insufficient in use, the fallback is prefixing the task name in the start
group rather than adding an event series.

**`start_at` is write-only in practice.** No code path produces it and
nothing consumes it specially — it round-trips and shows in fetches. If it
stays unused after a few months, the honest move is to drop it from the
write surface rather than keep a field that only ever holds hand-entered
values.

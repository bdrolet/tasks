# Measured capacity and the decide list

**Status:** approved in conversation 2026-10-16, ready for an implementation
plan. Amends `2026-10-12-must-do-ordering-design.md` D2 and
`2026-09-23-next-prioritizer-design.md` §Capacity / §Selection. Builds on
PR #50 (merged as `b1c9342`), which must stay in place.

This document is meant to be handed to an implementer with no other context.
Everything that was decided, and why, is here.

## Problem

The user asked for two things that the current `/next` cannot give together:

1. **Don't miss anything.** Every task that is due today or already overdue
   must be visible today.
2. **Only do what's needed.** Today's list must be a day of work, not a
   backlog.

The system has failed both ways in one week.

**Too long (before #50, 2026-10-15).** `POST /next` returned 34 rows / 66
points, 28 of them `overcommitted`. The `task-next` agent then merged the
`overcommitted` side list into its listing, which made 45 rows. Two causes:

- Must-dos (hard date, `effective_slack ≤ hard_due_slack_days = 5`) were placed
  past capacity. #50 fixed this.
- `effective_slack` comes from the feasibility pass, which stacks every
  hard-dated `next` task back to back in due order at
  `effort_days = points / points_per_day`. At `points_per_day = 5`, 66 points
  is about 13 working days, so a task due 10/30 was simulated to start around
  10/26 and counted as a must-do on 10/15. That is the cascade. Overdue dead
  dates sit at the front of the queue and make it worse.

**Too short (after #50, 2026-10-16).** `/next` returned 4 rows / 5 points. The
rest of what was due today or overdue (a birthday invite due today, a monthly
payment collection 6 days overdue, three school-paperwork tasks 15 days
overdue, a return shipment 30 days overdue) moved into `overcommitted` and
was no longer shown. The day is now honest about capacity but silent about
deadlines.

**Capacity is wrong.** `config/prioritize.toml [capacity] points_per_day = 5`
was a guess made when the prioritizer shipped. The user finishes far more
than that (measured below). Because capacity also drives `effort_days`, the
low number both makes the cascade and starves the selection.

### Measured throughput

From `task_stats`, completions grouped by local day (America/Los_Angeles).
A task's points are `coalesce(points_at_completion, points_estimated)`:

```sql
select (completed_at at time zone 'America/Los_Angeles')::date d,
       count(*) n,
       sum(coalesce(points_at_completion, points_estimated)) pts,
       count(*) filter (where coalesce(points_at_completion, points_estimated) is null) unpointed
from task_stats where completed_at is not null
group by 1 order by 1;
```

| day | closed | points | unpointed |
|---|---|---|---|
| 09-24 | 22 | 29 | 0 |
| 09-28 | 28 | 14 | 20 |
| 09-29 | 6 | 7 | 0 |
| 09-30 | 26 | 14 | 17 |
| 10-01 | 22 | 3 | 19 |
| 10-02 | 1 | 1 | 0 |
| 10-06 | 2 | 3 | 0 |
| 10-07 | 47 | 40 | 24 |
| 10-08 | 5 | 8 | 0 |
| 10-09 | 2 | — | 2 |
| 10-12 | 17 | 17 | 3 |
| 10-13 | 23 | 16 | 13 |
| 10-14 | 12 | 12 | 6 |
| 10-15 | 16 | 19 | 5 |
| 10-16 (partial) | 8 | 18 | 0 |

The week of 10-12 averages about 16 pointed points a day. The numbers need
reading with care:

- 09-24 and 10-07 were bulk-cleanup days (22 and 47 closes).
- Some closes are bookkeeping, not work. On 10-16 four tasks for one tax
  mailing closed together, about 12 points for one errand.
- Many closes carry no points.

## Decisions

### D1: Capacity is measured from completions

New pure module `services/capacity.py`:

```python
@dataclass(frozen=True)
class Capacity:
    points_per_day: float      # the value used for effort and selection
    source: str                # "measured" | "config"
    median: float | None       # median daily points over the window
    days: int                  # active days in the window
    unpointed: int             # completions in the window that carried no points
    window_start: date | None
    window_end: date | None

def measure(daily: list[tuple[date, int, int]], today: date, config: Config) -> Capacity:
    """daily = [(local_day, pointed_points, unpointed_count)] ..."""
```

Rule:

1. **Lookback.** Take completions from the `lookback_days` (28) local days
   before `today`. Exclude `today` itself, because it is partial.
2. **Active days.** An active day is one with at least one *pointed*
   completion. Keep the most recent `window_days` (14) of them.
3. **Unpointed tasks.** Unpointed completions are not counted as points. They
   are reported in `unpointed`. Excluding them can only make capacity too
   low, never too high.
4. **Too little history.** If there are fewer than `min_days` (5) active
   days, return the config fallback: `source = "config"` and
   `points_per_day = [capacity].points_per_day`.
5. **Formula.** Otherwise:
   `points_per_day = clamp(floor(factor × median + 0.5), floor, ceiling)`,
   with `factor = 0.75`, `floor = 5` and `ceiling = 20`.
   - The median ignores bulk-cleanup days.
   - The factor allows for bookkeeping closes, and for the fact that a day
     also holds unplanned work.
   - Round half up, not with Python's `round()`.
6. **Fixed mode.** `mode = "fixed"` skips measurement and always uses the
   config value. This keeps an escape hatch.

**Worked example on 2026-10-16.**
- The 28-day lookback runs 09-18..10-15, with 13 active days:
  1, 3, 3, 7, 8, 12, 14, 14, 16, 17, 19, 29, 40.
- The median is 14, and 0.75 × 14 = 10.5, which rounds to 11.
- As the sparse late-September days age out, the value will rise toward
  about 12–13 if the current pace holds.

New config keys, alongside the existing `points_per_day`, `default_points`,
`low_confidence_multiplier` and `min_effort_days`:

```toml
[capacity]
mode = "measured"        # measured | fixed
points_per_day = 5       # fallback, and the value in fixed mode
window_days = 14         # active days used for the median
lookback_days = 28       # calendar days searched for active days
min_days = 5             # fewer active days than this → fallback
factor = 0.75
floor = 5
ceiling = 20
```

`services/prioritize_config.py` loads them with these defaults when they are
absent. They change `config.fingerprint`, as any config edit does.

**Where it applies.** Measured capacity replaces `config.points_per_day`
everywhere it is read today:

- `services/prioritize.py` ~L420: `effort = eff.points / config.points_per_day`,
  the feasibility pass and urgency.
- `select()` L628/L631: the must-do and fill capacity checks.
- `_order_musts` L655: the effort fallback.

The simplest way is to make `rescore()` and `POST /next` call
`dataclasses.replace(config, points_per_day=capacity.points_per_day)` before
scoring or selecting. That way `services/prioritize.py` does not change its
signature.

**When it is computed and stored.**

- **Daily tick.** Only the daily tick (`handle_day_changed` →
  `rescore(kind="daily")`) measures capacity. It reads `task_stats` with a new
  `repo.daily_completions(conn, start, end)`, which runs the SQL above bounded
  to the lookback and returns `[(day, pointed_points, unpointed_count)]`.
- **Storage.** The result is stored on the run row:
  `ALTER TABLE prioritize_runs ADD COLUMN IF NOT EXISTS capacity JSONB;`
  (in `repo/schema.sql`), holding the `Capacity` fields as JSON.
- **Event runs.** `rescore(kind="event")` reuses the latest daily run's
  capacity, read by a new `repo.latest_capacity(conn)`. That keeps
  `effort_days` stable through the day; it does not change with each
  completion.
- **`POST /next`.** It reads `repo.latest_capacity(conn)` too, so its
  re-selection matches the stored scores.
- **No stored value yet.** This covers the first deploy and runs before the
  migration. Every reader falls back to `measure()` with `source = "config"`
  semantics, which means the config value. Nothing crashes because the column
  or row is missing.
- **DB failure.** `handlers/prioritize.py` already raises on a DB failure so
  Pub/Sub redelivers (CLAUDE.md layer rules). Measurement is part of that
  transaction and needs no separate degrade path.

Raising capacity also shrinks the cascade on its own. At 11 points a day the
same 66-point queue is about 6 days, not 13.

### D2: `/next` returns a complete `decide` list

`NextResponse` (in `api/routers/next.py`) gains three fields. The existing
fields are unchanged, so current callers keep working.

```python
class NextResponse(BaseModel):
    ...                         # today, scored_at, run_id, next, overcommitted, stale, nudge, unenriched
    capacity: dict              # Capacity as JSON, plus "used": points in `next` excluding pins
    decide: list[DecideTask]    # RankedTask + decide_reason
    starting_soon: int
```

**`decide`.** These are tasks with a hard date of today or earlier that are
not in today's `next`. Each one needs the user to choose: do it anyway,
snooze it, re-date it, or drop it. A task is included when all of these hold:

- `components["due_source"] == "hard"` and `components["days_until_due"] <= 0`.
  Subtasks inheriting an ancestor's hard due date count; see #43.
- It is not in `next` after `select()`, pins included.
- Its bucket is one of the following:
  - `next`, with `decide_reason = "over_capacity"`;
  - `nudge`, with `decide_reason = "waiting"` (show `waiting_on`);
  - `excluded:blocked`, with `decide_reason = "blocked"`;
  - `stop_doing`, with `decide_reason = "serves_nothing"`. This bucket exists
    only in necessity `suppress` mode, and the rule costs nothing otherwise;
  - `excluded:parent`, with `decide_reason = "parent"`, but **only if** none
    of its open descendants is already in `next` or `decide`. Otherwise the
    subtasks represent it, and listing the parent would duplicate them.
- Never included:
  - `snoozed`: the user already decided.
  - `excluded:completed` and `excluded:project`: these are not the user's
    work in a managed project.

Order:

- Due-today tasks (`days_until_due == 0`) come first, by `cost_of_delay`
  descending. They can still be saved.
- Then overdue tasks, by `cost_of_delay` descending. This is the same rule as
  #50's late tier.

`DecideTask` is `RankedTask` plus `decide_reason: str`.

**The invariant, covered by a property test.** Every task that meets all of
the following appears in exactly one of `next` or `decide`, never both and
never neither:

- open;
- not snoozed;
- in a bucket other than `excluded:completed` or `excluded:project`;
- has a hard date with `days_until_due <= 0`.

The one exception is the `excluded:parent` case in the rule above.

**`starting_soon`.** This is the count of tasks in bucket `next` that are not
selected, not pinned, `_is_must()`, and `days_until_due > 0`. These are the
cascade tasks: real deadlines whose queue slack is short, but not due today.
`/next` reports them only as a count. Their rows remain available in
`GET /ranking` and `GET /ranking?list=overcommitted`.

`decide` is computed in a new pure function `services/prioritize.py::decide(
scored: ScoredSet, picked: list[ScoredTask]) -> list[tuple[ScoredTask, str]]`,
next to `side_lists`. Keeping it pure means it can be tested without the API.
Add `decide` to `LISTS` only if `GET /ranking?list=decide` is wanted. It is
not required.

### D3: The system decides nothing on the user's behalf

`decide` is a list, not an action. Nothing in this spec snoozes, re-dates,
completes or drops a task. This follows the user's standing rule that a task
is dated only when a real external deadline exists, and that a date they
cleared is never restored. The weekly review's `stale_dates` section
(#51, open) stays the place where dead dates are proposed for clearing.

### D4: `task-next` renders today, decide, and a count

Update `.claude/agents/task-next.md` and
`.claude/skills/prioritizing-tasks/SKILL.md`. Keep the two in sync; CLAUDE.md
requires both paths to produce the same listing format. The default answer to
"what should I do next" / "what are my next tasks" becomes:

```
**Today** (<used> of <capacity> points · capacity <source>: median <m> over <days> days)
<ref> · <gid> · <priority> · <due> · <points>p
  [<name>](<permalink_url>) · <project> · <flags>
  <one-line reason>
...

**Due or overdue, didn't fit: decide** (<k>)
<ref> · ... · <decide_reason in words: over capacity / waiting on X / blocked by Y / serves nothing / parent>
  → do it, snooze it, re-date it, or drop it
...

<starting_soon> more hard-dated tasks should start this week (ask to see them).
```

Rules for the agent:

- Do **not** merge `overcommitted`, `stale` or `nudge` into the listing. That
  merge is what produced the 45-row day. Mention their counts in one line
  only when asked or when non-zero and relevant.
- Omit the decide section when it is empty. Always show the capacity line.
- Keep the existing ref-first format, the `task-ref` piping and the closing
  ref → GID map.

The skill's API reference section documents the new response fields.

### D5: Documentation

- `CLAUDE.md`, Prioritizer row of the Stack table: add a link to this spec.
- `2026-10-12-must-do-ordering-design.md` D2 says must-dos that don't fit are
  "visible as positions in the ranking and … in `overcommitted`". Add a line
  pointing here: due-today and overdue ones are now in `decide`.
- `2026-09-23-next-prioritizer-design.md`: note in §Capacity that
  `points_per_day` is now the fallback for measured capacity.

## Non-goals

- **Necessity `flag` → `demote`.** This is the other lever for "only do
  what's needed". Wait until `GET /calibrate` shows enough necessity agreement
  to justify it, then give it its own spec.
- **Filtering bookkeeping closes out of throughput**, for example sibling
  tasks closed within minutes of each other. The median and factor absorb
  this well enough. Revisit only if capacity is visibly wrong.
- **Per-weekday or per-energy capacity.**
- **Any automatic date, snooze or completion change.** See D3.
- **Changing `_is_must`, scoring weights, pins or the fill.**

## Files

| File | Change |
|---|---|
| `services/capacity.py` | new: `Capacity`, `measure()` |
| `services/prioritize_config.py` | load the new `[capacity]` keys with defaults |
| `config/prioritize.toml` | add the keys above |
| `services/prioritize.py` | `decide()`; no signature changes elsewhere |
| `repo/schema.sql` | `prioritize_runs.capacity JSONB` |
| `repo/prioritize.py` | `daily_completions()`, `latest_capacity()`, `insert_run(..., capacity=)` |
| `handlers/prioritize.py` | `rescore()`: measure on daily, reuse on event, `replace(config, ...)`, store on the run |
| `api/routers/next.py` | read capacity, `replace(config, ...)`, add `capacity`, `decide`, `starting_soon` to `NextResponse` |
| `.claude/agents/task-next.md`, `.claude/skills/prioritizing-tasks/SKILL.md` | D4 |
| `CLAUDE.md`, the two amended specs | D5 |

## Testing

- **`tests/test_capacity.py`:**
  - fallback below `min_days`;
  - `mode = "fixed"`;
  - today excluded;
  - lookback bound;
  - only the most recent `window_days` active days are used;
  - the median ignores one 40-point outlier;
  - round half up (10.5 → 11);
  - floor and ceiling clamp;
  - unpointed completions counted in `unpointed` and not in points;
  - the 2026-10-16 worked example gives 11.
- **`tests/test_prioritize.py`:**
  - `decide()` membership for each bucket and reason;
  - the parent rule;
  - ordering, due-today before overdue;
  - the exactly-one-of invariant as a property test over generated
    `ScoredSet`s, in the style of the existing `select()` tests;
  - the `starting_soon` count.
- **`tests/test_api_next.py`:**
  - the response carries `capacity`, `decide` and `starting_soon`;
  - the capacity is read from the latest daily run;
  - fallback when no run has a capacity, or when the column is NULL;
  - selection uses the measured value, so a fixture with capacity 11
    selects more than 5 points.
- **Handler test.**
  - A daily rescore stores capacity on its run.
  - An event rescore reuses it and does not re-measure.
- **Local check.** Follow the `verifying-pr-locally` skill:
  - run tasks-api locally against the real DB;
  - `POST /next` should show capacity of about 11 and a `decide` list that
    includes every hard-dated task due today or earlier;
  - cross-check that list against `GET /ranking`.

## Rollout

1. Branch off `main`, implement, and open the PR with `/pr-open`. Auto-deploy
   watches `main`: Deploy API and Deploy Functions both run on merge.
2. Apply the schema before or with the merge (`scripts/migrate_db.py`; see the
   README). Code must tolerate the column being absent until then.
3. Capacity takes effect at the next `tasks-day-changed` tick (05:45
   America/New_York). Until then every reader uses the config fallback of 5.
   To pick it up immediately, publish a `day_changed` message by hand or run
   the scheduler job.
4. After a week, compare `capacity.points_per_day` with the points actually
   completed per day. Tune `factor` in config if they disagree. That is a
   config change, not code.

## Related

- `2026-09-23-next-prioritizer-design.md`: base design, capacity, feasibility.
- `2026-10-08-hard-deadlines-over-inferred-waits-design.md`: hard dates
  release model-inferred waits.
- `2026-10-12-must-do-ordering-design.md` (#50): the on-time and late tiers,
  and must-dos stopping at capacity.
- PR #51 (open, stacked on #50's old branch; retarget to `main` before
  merging): `stale_dates` in the weekly review.

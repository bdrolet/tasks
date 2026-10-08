# Hard deadlines over inferred waits

**Date:** 2026-10-08
**Status:** designed, not implemented
**Amends:** `2026-09-23-next-prioritizer-design.md` (D9, D11, D14, D16)

## Problem

On 2026-10-08 the two 2025 tax filings, both `due_on` 2026-10-15 and each a
day of work, were absent from `/next` and had been since the prioritizer went
live. Neither was snoozed, blocked, pinned or in an excluded project. Three
things hid them, each on its own enough:

1. **A model-inferred wait counts the same as one Ben set.** The federal
   return sat in `nudge` as "waiting on Michael (CPA)". The model read a
   comment asking Ben to send Renee his 2025 documents and called that a
   wait. Nothing distinguished this guess from a `waiting:` tag, and there
   was no way to say "not waiting": `PUT /overrides {"waiting_on": null}`
   deletes the override, so the model's value comes straight back. The only
   way out was a pin.
2. **A parent's wait reaches every subtask.** The California return is a
   subtask of "Resolve the FTB balance across 2022–2024", whose own inferred
   wait ("FTB mailing the installment agreement form") D16 inheritance
   applied to the return. The form gates the payment plan, not the filing.
3. **The must-do window ignores effort.** The score is cost of delay ÷
   effort, and cost of delay tops out at 1.0, so a day-sized task cannot
   score above ~1.0 while the greedy fill's seventh pick scored 1.89. That is
   WSJF working as designed: big hard-dated work reaches the list only
   through the must-do rule, and that rule fired at `days_until_due ≤ 1`
   whatever the size. A day of work got one day's notice.

Pinning both tasks would have fixed today. It would not have stopped the
next confident wrong guess, and it is not the system's job to make Ben
audit its inferences against his deadlines.

## Goals

- A task with a hard `due_on` surfaces early enough to do the work, scaled
  to how much work it is.
- A wait the model inferred can never hide a hard deadline. A wait Ben set
  (tag or override) still can, because that is an instruction.
- A parent's inferred wait reaches none of its subtasks. A parent's
  hand-set wait reaches all of them, as D16 intended.
- "This task is not waiting" is something Ben can say once and have stick.

## Non-goals

- No change to the cost-of-delay weights, the urgency curve, capacity, or
  the greedy fill. The fix is at the must-do boundary and in what a wait is
  allowed to do, not a general reshuffle.
- No change to snooze or blocked inheritance, to pins, or to due-date
  inheritance (`c59b4d8`).
- No new side list. Released tasks are ordinary `next` rows with a flag.
- No change to what becomes a task or to enrichment of email tasks.

## Decisions

### D1 — A wait carries a source and, from the model, a confidence

The enrichment schema (`services/enrichment.py::SCHEMA`) gains a required
`waiting_confidence: low | medium | high`, and `Enrichment` gains the field.
The system prompt's `waiting_on` paragraph becomes:

> waiting_on — the external party (a person, company or process) who must
> act before Ben can do anything more, or null. Waiting means Ben has done
> his part. A task Ben has not started is not waiting. A task whose newest
> comment asks Ben to send, sign, provide or decide something is not
> waiting — that is his action. waiting_confidence is high only when a
> comment says the wait outright ("sent to X, waiting on their reply");
> medium when the wait is implied by the task's state; low when you are
> reading between the lines.

`services/prioritize.py::Effective` gains `waiting_source: tag | override |
model | none`, set by the same precedence that picks the value (tag >
override > model). A model wait with `low` confidence is dropped at this
point — `waiting_on = None`, `waiting_source = "none"` — the same treatment
a low-confidence inferred date already gets. `components` records both
`waiting_source` and `waiting_confidence` so `--explain` shows them.

A stored enrichment row without `waiting_confidence` (every row written
before this change) reads as `medium`, which is today's behaviour, until it
is re-enriched (§Rollout).

### D2 — "Not waiting" is an override value

`PUT /tasks/{gid}/overrides {"waiting_on": ""}` stores the empty string.
`null` keeps its meaning: clear the override, fall back to the model.
`Effective.pick` already treats `""` as a value, so `own.waiting_on` is
falsy and the task never buckets as waiting; `waiting_source` is
`override`. An own `waiting:` tag still wins over it; an ancestor's
hand-set wait does not — the child's explicit answer is the more specific
one.

`task-next override <ref> waiting_on=-` sends `""` (`waiting_on=` with
nothing after it already means clear and keeps meaning that; `-` is the
listing's own marker for "none"). The agent maps "X isn't waiting on
anyone" / "I'm not waiting on that" to this call.

### D3 — Only hand-set waits inherit

`_State` gains `waiting_source`. In `_bucket`, the ancestor walk for
waiting takes the first ancestor whose `waiting_on` is set **and** whose
`waiting_source` is `tag` or `override`. A model wait on a parent stays on
the parent — which is `excluded:parent` anyway — and never reaches a
child. Precedence across levels: a hand-set wait beats a model wait at any
level, and among hand-set waits the nearer wins. A task's own `tag` or
`override` decides outright and the ancestor walk is never consulted — an
own `""` override (D2) means not waiting and stops the walk. Otherwise the
nearest hand-set ancestor wait is inherited, even over the task's own model
wait (and, being hand-set, it is never released by D4). Only when no
hand-set wait exists at any level does a task's own model wait bucket it
(subject to D4).

Snooze, blocked and hard-due inheritance are unchanged.
`components["inherited"]` keeps its shape and fires less often.

### D4 — A hard deadline releases a model wait

After `_bucket` and `_effective_due`, in `score_set`: a task bucketed
`nudge` whose `waiting_source` is `model`, whose `due_source` is `hard`
(own `due_on` or an inherited one), and whose raw slack
`days_until_due − effort_days` is `≤ hard_due_slack_days` (D5) is
re-bucketed to `next`. Its `waiting_on` stays on the row, and
`components["wait_released"] = {"waiting_on": <who>, "slack": <days>}`
records why it is there. A `tag` or `override` wait is never released. Snooze
and blocked are untouched by this rule. A task whose hard date has already
passed (`days_until_due < 0`) is not released: the rule exists so a deadline
surfaces *before* it passes, and a past-dated model wait is a stale nudge
(an old meeting, a closed registration), not an imminent deadline —
releasing those floods the EDF queue with overdue must-dos. One still
genuinely needed is rescued with `waiting_on=-` or a pin. Due today is not
past and is released.

Raw slack, not EDF `effective_slack`, because the task was not in the
feasibility queue when bucketed; once released it joins the queue like any
other hard-dated `next` task and gets an `effective_slack` of its own.

The CLI and the agent render a released row in **Next** with the flag
`waiting?<who>` — a question mark because the system is saying "the model
thought this was waiting, but the deadline is close; decide." Ben's
answers are the existing moves: `waiting_on=-` (not waiting; D2), or
`waiting_on=<who>` (it really is; the override holds and the task returns to
nudge), or just do it.

### D5 — Must-dos are decided by slack, not calendar days

`[selection] hard_due_window_days` is replaced by `hard_due_slack_days`
(default **5**). `_is_must` becomes:

```
due_source == "hard" and effective_slack is not None
    and effective_slack <= hard_due_slack_days
```

`effective_slack` is already stored in `components` — the EDF result for
hard-dated `next` tasks, so a task queued behind other hard work surfaces
sooner — and `POST /next` re-selects from stored rows unchanged. Overdue
tasks have negative slack and stay must-dos. Order within must-dos stays by
score. Nothing else in selection (pins, capacity, fill, starvation,
diversity) changes.

Why 5: for day-sized work it is "about a week before"; for an hour-sized
task it is a few days. Must-dos bypass `n` and capacity, so several big
deadlines in one week will stack on the same day — which is the right
signal and what `overcommitted` already reports.

Worked example, today 2026-10-08, both filings 5 points due 10-15:

| Task | Wait | Raw slack today | Today (10-08) | 10-09 |
|---|---|---|---|---|
| Federal return (own model wait) | released once raw slack ≤ 5 (D4) | 7 − 1 = 6 | nudge | released → `next`; must-do |
| CA return (parent's model wait) | not inherited (D3) | 7 − 1 = 6 | `next`, ~position 93 | must-do |

On 10-09 both are hard-dated `next` tasks six days out with a day of work
each; the EDF pass gives the first slack 5 and the second slack 4, so both
are must-dos. Before this change the first notice would have been 10-14.

### D6 — Schema changes invalidate the enrichment cache

`services/enrichment.py` prefixes the content hash with a schema version:
`sha256("v2\n" + name + "\n" + notes + "\n" + comments)`. The version is
in the hash so that a re-gathered task computes a new hash, finds it ≠ the
stored one, and re-enriches. The bump alone does not reach an untouched
task — the heal (D8 of the original spec) compares two stored values, both
written under v1 — so `heal` also republishes any task whose stored
enrichment `raw` lacks a key the current `SCHEMA` requires. The next
`day_changed` therefore re-enriches every pre-v2 row with the new prompt
and schema — ~200 Opus calls once, a few dollars, no backfill script.
Until a task is re-enriched, D1's default (`medium`) keeps its current wait; D3 and D4 protect hard-dated tasks regardless.

## Changes by component

| Path | Change |
|---|---|
| `services/enrichment.py` | `waiting_confidence` in `SCHEMA` (required) and the pydantic model; prompt paragraph (D1); hash version prefix (D6); parse missing confidence as `medium` |
| `models/prioritize.py` | `Enrichment.waiting_confidence: str` (`DEFAULT`: `"low"`, moot with `waiting_on=None`) |
| `services/prioritize.py` | `Effective.waiting_source`; low-confidence model wait dropped in `effective()`; `_State.waiting_source`; `_bucket` inherits tag/override waits only (D3); release step in `score_set` (D4); `_is_must` on `effective_slack` (D5); new `components` keys `waiting_source`, `waiting_confidence`, `wait_released` |
| `services/prioritize_config.py`, `config/prioritize.toml` | `hard_due_slack_days` replaces `hard_due_window_days` (a config still naming the old key fails to load — it is a rename, not an alias) |
| `api/routers/next.py` | `OverridesRequest.waiting_on` accepts `""`; `TaskRow` gains `wait_released: bool` (from `components`) so callers need not ask for `explain` |
| `scripts/task_next.py` | `waiting_on=-` → `""` (D2); `waiting?<who>` flag on released rows; must-do flag reads `due in Nd` / `due today` / `due tomorrow` / `overdue` from `days_until_due` (flag tokens space-joined) |
| `.claude/agents/task-next.md`, `.claude/skills/prioritizing-tasks/SKILL.md` | the `waiting?` flag and what to do with it; "not waiting" phrasing → `waiting_on=-`; must-do wording; inheritance note (hand-set waits only) |
| `docs/superpowers/specs/2026-09-23-next-prioritizer-design.md` | one-line "amended by this spec" pointers under D9, D11, D14, D16 |
| `docs/prioritize-audit.md` | mention `wait_released` in the components list if it enumerates them |

No schema migration: `components` is JSONB and the new keys are additive;
`task_enrichment.raw` is verbatim JSON.

## Config

```toml
[selection]
default_n = 5
diversity_penalty = 0.95
energy_penalty = 0.7
hard_due_slack_days = 5       # hard due_on with effective_slack <= N is a must-do (beyond n and capacity);
                              # the same N releases a model-inferred wait on a hard-dated task back into next
```

## Read side

`GET /ranking` and `POST /next` rows gain `wait_released` (boolean, default
false). `components` (with `explain`) gains `waiting_source`,
`waiting_confidence` and `wait_released: {waiting_on, slack} | null`.

`PUT /tasks/{gid}/overrides`: `waiting_on` may be `""` (not waiting),
a string (waiting on them), or `null` (clear the override). No other field
changes.

## Testing

`tests/test_prioritize.py`:

- a model wait with `low` confidence does not bucket to nudge; `medium` and
  `high` do; a `waiting:` tag or override does regardless of confidence
- `waiting_on = ""` as an override: not waiting, `waiting_source = override`;
  a `waiting:` tag beats it
- a subtask inherits a parent's tagged or overridden wait; it does **not**
  inherit the parent's model wait; snooze and blocked still inherit (D3)
- release (D4): a hard-dated task with a model wait and raw slack ≤ N is in
  `next` with `wait_released` set and `waiting_on` kept; the same task with
  slack N+1 is in nudge; the same task with an override wait is in nudge at
  slack 0; an inferred/horizon date never releases; an inherited hard date
  does
- must-do (D5): a hard-dated task with `effective_slack ≤ N` is selected
  beyond `n` and capacity; at N+1 it is not; two tasks due the same day where
  only the second's EDF slack is ≤ N — the second is the must-do; an overdue
  task is a must-do; `POST /next` re-selection from stored components agrees
- the existing D16 waiting-inheritance test is split into the tag and model
  cases above

`tests/test_enrichment.py`: a response without `waiting_confidence` fails
validation; a stored row without it parses as `medium`; the hash differs
from the pre-version hash for the same content.

`tests/test_api_next.py`: overrides accept `""` for `waiting_on` and store
it; `null` clears; rows carry `wait_released`.

`tests/test_task_next.py`: `waiting_on=-` sends `""`; `waiting_on=` sends
`null`; a released row renders `waiting?<who>`; a must-do five days out
renders `due in 5d`.

`tests/test_prioritize_config.py`: `hard_due_slack_days` loads; a config
with only `hard_due_window_days` fails.

## Rollout

1. Deploy (`tasks-prioritize` CF and `tasks-api`), with the config rename
   in the same change.
2. The next `day_changed` tick (05:45 ET) heals every task whose stored
   enrichment lacks a field the v2 schema requires (all of them, D6) and
   re-enriches it — ~200 calls once; a re-gathered task re-enriches anyway
   because its v2 hash differs from the stored v1 one. Until then,
   stored waits read as `medium` and D3/D4 already apply to the next
   rescore, so the filings surface on the first event after deploy — or
   `scripts/backfill_prioritize.py` forces it.
3. Watch `asana_prioritize_enrich_total{result}` for the one-off burst and
   `GET /ranking?list=nudge` for the day after: the list should be shorter
   (low-confidence waits gone) and no hard-dated task within N days of its
   slack should be in it.

## Follow-ups (not in this spec)

- A `waiting_since` on the override, so nudge can age a wait and suggest a
  follow-up — "waiting on Michael for 12 days" is a nudge in its own right.
- The original spec's open item on `days_stale` being bumped by this
  service's own writes still stands.
- `calibrate` could report how often a released wait was overridden back to
  waiting, which is the measure of whether `waiting_confidence` is honest.

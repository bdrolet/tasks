# Must-do ordering and capacity

**Status:** accepted 2026-10-12. Amends `2026-09-23-next-prioritizer-design.md`
§Selection and `2026-10-08-hard-deadlines-over-inferred-waits-design.md` D5.

## Problem

On 2026-10-12 the Next list held 58 must-dos (hard-dated, `effective_slack ≤ 5`),
53 of them overcommitted. Two five-point tax returns due in three days sat at
ranks 54 and 55. Three things combined:

1. **Must-dos were ordered by score** (cost of delay ÷ effort). When every
   must-do is late or nearly so, urgency is saturated at 1 for all of them, so
   score reduces to priority × impact ÷ effort — a one-point overdue errand beats
   a day of work due Thursday.
2. **Must-dos bypassed capacity.** All 58 were "selected", which is a list, not
   a day.
3. **Dead dates.** Meetings that already happened and self-set dates that passed
   still count as hard and late. In the EDF feasibility pass each one pushes the
   cursor out, so everything behind it reads as overcommitted.

Cost of delay alone does not fix it either: measured against the live ranking,
both returns tie at 0.68 with a dozen other P1 tasks, behind two dead-dated
meetings. Pure earliest-deadline-first does not fix it: a P2 blog draft due
Wednesday consumes the schedule ahead of the federal return due Thursday.

## Decision

### D1 — Two tiers: on time, then late

`select()` splits must-dos into an **on-time** tier and a **late** tier.

The on-time tier is built by walking must-dos in due order (`days_until_due`
ascending, ties by `cost_of_delay` descending) with a running cursor of
`effort_days`. A task is on time when it would finish by the end of its due
day: `cursor + effort_days ≤ days_until_due + 1`. When adding a task would make
it late, the task with the **lowest `cost_of_delay`** in the on-time set plus
the newcomer is removed, repeatedly, until the newcomer fits or is itself the
one removed. Removing an earlier task never makes a later one late, so the set
stays consistent.

Everything removed, and everything already overdue (`days_until_due < 0`, which
can never finish by its due day), is the late tier, ordered by `cost_of_delay`
descending: a deadline that has already passed has no cliff left, so only how
much being later hurts distinguishes them.

Why weighted removal rather than skipping the newcomer: a tax return with a
statutory penalty behind it outranks a blog draft when both cannot be made on
time, and priority × impact is the only signal the system has for that.

### D2 — Must-dos respect capacity

Must-dos are taken on-time tier first, then late tier, each while
`used < points_per_day`, the same rule the fill uses. They still bypass `n`,
energy and diversity. Must-dos that do not fit are not selected; they are
visible as positions in the ranking and, where their EDF slack is negative, in
`overcommitted`. Nothing changes in scoring, positions, pins, the fill, or
`_is_must`.

### D3 — Dead dates are data, not code

No ordering rule can tell a dead date from a live one. D1 keeps dead dates out
of the on-time tier (they are overdue), which stops them poisoning the day's
pick, but they still inflate `effective_slack` in the feasibility pass and the
`overcommitted` list. Clearing them is a review with the user; the system never
restores a date the user cleared.

### D4 — The weekly review lists stale dates

Nothing surfaced dead dates: the review's grooming section is about
`serves:` tags, "Groom the outreach pipeline" is about outreach, and the
Inbox sweep is the Inbox project. So `GET /review` and the Monday comment
gain a `stale_dates` section: open tasks (any non-excluded bucket) with
`due_source = hard` and `days_until_due ≤ −3` that carry no `repeat:` tag
(a cadence date that slipped is work to do, not a date to clear), grouped
under the task that owns the date — a subtask inheriting its parent's
`due_on` is listed under the parent, since the parent is what to clear —
ordered by days overdue. Each row says "clear the date or complete it";
the system never does either on its own, and never restores a cleared
date. `repo.list_scores` now carries `task_facts.tags` so the review can
see the `repeat:` tag.

## Measured outcome (2026-10-12 ranking, before any date cleanup)

| Rule | Day's pick |
|---|---|
| score (before) | 58 tasks; returns at 54/55 |
| cost of delay | dead-dated P0 meeting, a meeting tomorrow, outreach grooming (overdue) |
| EDF, skip late | mediation prep (5 pt, due today); federal return falls to the late tier behind a P2 blog draft |
| **D1+D2** | mediation prep (5 pt, due today). On-time tier continues: meeting tomorrow, CA return, federal return, JPM invite, FL-142 list. The P2 blog draft and job-search sweep are displaced to the late tier |

Both returns are in the on-time tier and reach the pick as soon as the task
due today is done or its self-set date is moved.

## Files

| File | Change |
|---|---|
| `services/prioritize.py` | `_order_musts()`; `select()` takes must-dos through capacity |
| `tests/test_prioritize.py`, `tests/test_api_next.py` | tier order, weighted removal, capacity cap, stored-row re-selection |
| `docs/superpowers/specs/2026-09-23-next-prioritizer-design.md`, `…2026-10-08-…-design.md` | amendment notes |
| `.claude/skills/prioritizing-tasks/SKILL.md`, `.claude/agents/task-next.md`, `config/prioritize.toml` | wording |

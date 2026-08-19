# Standing-Context Gate — Design

**Date:** 2026-08-18
**Revised:** 2026-08-19 — after review
**Status:** Pending review

## Goal

Stop creating tasks that are correct on the email's own terms but moot given a
fact about Ben that lives nowhere in the mail stream — most immediately, mail
addressed to a role he has resigned.

Today `handlers/task_create.py` decides from one event in isolation:

```python
if not policy.warrants_task(event):   # category in {urgent, review, respond}
    return
```

That is the whole gate. This spec adds a **second gate**, after enrichment,
that asks one question: *given what is declared true about Ben right now, does
this email still require anything of him?*

It also introduces the declared-facts file that gate reads, and gives
`services/deadline.py` a second section of the same file to read.

## The evidence

Ben resigned as Assistant Coach of the West Portal Proud Panthers on
2026-08-14. SF Microsoccer's admin mail continues, and he wants to keep
receiving it — he is still the parent of a player on that team. He does not
want it becoming action items.

Three Micro Admin emails have already produced three P1 tasks:

| Email | Task |
|---|---|
| 7/31 "Microsoccer Game Schedule Draft Ready for Review" | `[P1] Review microsoccer schedule draft by August 4` |
| 7/31 — *the same email* | `[P1] Review draft microsoccer game schedule` |
| 8/11 "Microsoccer Game Schedule Ready" | `[P1] Review microsoccer game schedule and register for meetings` |

**Those tasks were correct.** In July Ben was the assistant coach; a draft
schedule to review by Aug 4 was a real obligation with a real deadline. The
next identical email will be wrong. Nothing observable about the email changes
between the correct case and the wrong one — sender, subject, body, category
and importance are all the same. Only Ben changed.

This is the defining property of the problem and the reason it cannot be solved
by matching on the email.

## Why matching cannot work

The obvious fix is a mute list keyed on sender. The mail says otherwise. Club
and league mail reaches Ben from at least six addresses across four domains:

| Sender | Carries | Actionable now? |
|---|---|---|
| `micro@sfvikings.com` | Micro Admin broadcasts | **No** — the target |
| `Lee@sfvikings.com` | "Elijah Drolet has been invited to join West Portal Proud Panthers" | **Yes** — parent-side |
| `noreply@sfvikings.org` | Account confirmations, player invites (note: `.org`) | Mixed |
| `noreply@fs18.formsite.com` | Coach registration results | Was, no longer |
| `khannegan@`, `aarongoose@`, `pauljthompson@`, `yalexander@` (gmail) | Practice cancelled, snack signups, game today | Mixed — personal addresses |
| `noreply@groups.google.com` | Team Google Group invitations | — |

Two failure modes, and the second is disqualifying:

1. **Under-inclusive.** Muting `micro@sfvikings.com` leaves the next blast from
   `Lee@` or a fresh `noreply@sfvikings.org` untouched. The address carrying
   the noise has already changed once.
2. **Over-inclusive.** `Lee@sfvikings.com` sends *both* admin traffic and
   Elijah's player invitations. A domain mute on `sfvikings.com` swallows mail
   about a child's team placement.

Subject matching fares no better: the team is renamed annually — Prowling
Panthers (SP25) → Speedy Panthers (2025-26) → Proud Panthers / "West Portal
Panthers" (2026).

The discriminator is Ben's **role**, which is not a string in the email.

## Relationship to the existing false-positive docs

`docs/more_context_needed.md` and `docs/no_action_needed_example.md` already
log eight false positives in two families. This is a third:

| Family | Missing evidence | Where it lives |
|---|---|---|
| More context needed | Prior mail, or an existing Asana task | In systems the pipeline can query |
| No action needed | Nothing — the thread itself is enough | In the email |
| **Standing context** (this spec) | A fact about Ben's roles and relationships | **Only in Ben's head until declared** |

The third family is the only one that cannot be derived. It must be written
down. That is the whole mechanism.

## The context file

**`context/standing-context.md`** — a new top-level directory, sectioned by
consumer.

```markdown
# Standing context

## Roles

### Assistant Coach, West Portal Proud Panthers (SF Microsoccer / SF Vikings)
**Ended 2026-08-14, for the 2026 fall season — that season runs 2026-09-12 to
2026-12-18.** Ben resigned; Christy Dillon is handling the replacement.
Elijah remains a player on the team.

- Coach- and admin-directed mail — schedules to review, coach admin
  requirements, Micro Admin broadcasts — is NOT actionable for this season.
- Mail about Elijah as a player — invitations, rosters, parent logistics —
  IS actionable.

## Calendar

- SFUSD 2026-27 fall term: 2026-08-17 to 2026-12-18.
- West Portal Elementary day: 7:50am-2:05pm Mon/Tue/Thu/Fri, 7:50am-12:50pm Wed.
```

**Why a directory and not `docs/`:** `terraform/cloud_functions.tf` excludes
`docs` from the Cloud Function source archive, so a file there would never
reach the running function. The archive's `excludes` is a denylist, so a new
top-level `context/` ships by default. This also keeps small runtime data out
of `docs/`, which holds plan documents in the hundreds of KB that have no
business in the function.

**Required companion change:** `.github/workflows/deploy.yml`'s `paths:` filter
is an allowlist. `context/**` must be added to it, or fact edits merge to
`main` and silently never deploy. This is a one-line, one-time change; it is
called out here because forgetting it produces a silent no-op on the one file
whose entire purpose is changing behavior.

**Why prose, not config:** the consumer is a model. Prose can express the
`Lee@sfvikings.com` distinction — same sender, opposite verdicts, decided by
what the mail is *about* — that no schema can encode. It is also the format
`docs/more_context_needed.md` and `docs/no_action_needed_example.md` already
use.

**Sections are addressed by heading.** `standing_context.section("Roles")`
returns that section's body, or `""` if absent. Each consumer reads only what
it needs: the task gate reads `Roles`, deadline extraction reads `Calendar`.
This keeps per-call token cost proportional to relevance rather than to the
file's total size.

Retiring a fact is deleting its block. There is no expiry field to maintain and
no matcher to operate on.

### Facts carry their own scope

**A fact that applies only for a bounded period states that period in its own
prose**, as the coach block above does ("for the 2026 fall season — that season
runs 2026-09-12 to 2026-12-18"). The model is given today's date and decides
whether the fact still applies.

This is the primary defence against stale facts, and it is preferred over any
external expiry mechanism because it requires no machinery, no scheduled job,
and no human acting on a reminder. A fact whose window has closed simply stops
being applied, and the failure direction is over-creating tasks — the
survivable one.

**A fact with no stated end is perpetual.** That must be a deliberate choice by
the author, not an oversight: some facts genuinely have no season ("Ben is no
longer a customer of X"), and inventing an end date for them is worse than
stating none.

**This imposes a hard requirement on gate 2's prompt:** it must include today's
date. `services/deadline.py` already opens with `Today is {today}`;
`services/email_summary.py` does not pass a date at all. Gate 2 rides on the
summary call, so scoped facts are uninterpretable without adding it. This is a
one-line change and a precondition for the whole approach, not an enhancement.

## Design

### Gate 2 — task suppression

```
policy.warrants_task(event)               gate 1 — category, free, unchanged
email_summary.generate(event)             Haiku call — already happens
policy.survives_context(summary, event)   gate 2 — NEW
asana.create_task(...)
```

Gate 2 sits **after** enrichment for three reasons:

1. It costs nothing for mail that never passes gate 1.
2. The enrichment output is itself evidence. `no_action_needed_example.md`
   already specifies that a generated "no action required" key point should
   veto creation; that check belongs at this same point.
3. It can ride on the Haiku call that already runs, avoiding a second
   round-trip — the same trade-off, decided the same way, as the title
   enrichment in `2026-07-20-task-title-enrichment-design.md`.

`services/email_summary.py` currently asks Haiku for
`{"key_points": [...], "title": "..."}`. When the `Roles` section is non-empty,
the prompt gains that section and two output fields:

```json
{
  "key_points": ["..."],
  "title": "Verb object",
  "actionable": true,
  "actionable_reason": "..."
}
```

Prompt language, in substance: *"Today is {today}. Below are standing facts
about the recipient. Some state the period they apply to; a fact whose period
has passed does not apply. Set `actionable` to false ONLY if a fact that
currently applies clearly makes this email require nothing of him, and name
that fact in `actionable_reason`. If no fact clearly applies, set `actionable`
to true. Do not reason beyond the facts given."*

The `Today is {today}` line is **required**, not decorative — scoped facts are
uninterpretable without it, and `email_summary.generate` passes no date today.

When the `Roles` section is empty or the file is absent, the prompt is
unchanged and the gate is skipped entirely — zero cost, zero behavior change.
That is the state the repo ships in until the first fact is added.

### Deadline context

`services/deadline.py` makes a Sonnet call for P0/P1 mail asking for an
explicit deadline. It is given today's date and nothing else, so relative
deadlines that depend on Ben's calendar ("before school starts", "by the end
of the fall session") resolve to `null`.

The `Calendar` section is injected into that existing prompt, ahead of the
current `Today is {today}` line. No new call, no new model, no change to the
return contract — still an ISO date or `null`.

Scope discipline: `Calendar` holds **dates and recurring schedules only**. It is
not a second place to describe roles, and the deadline prompt is not asked to
judge actionability. The two sections stay single-purpose so neither consumer
inherits the other's failure modes.

### Fail-open contract

The gate may only ever **remove** a task it is confident about. Every other
path creates the task:

- Missing `actionable` field → create.
- Unparseable JSON, or the whole Haiku call failing → create (this already
  happens today; `generate()` swallows exceptions and returns an empty
  `EmailSummary`).
- `actionable: false` with no `actionable_reason` → create. A suppression that
  cannot name its justification is not trustworthy enough to act on.
- Missing or unreadable `context/standing-context.md` → create.
- `event["category"] == "urgent"` → create, unconditionally.

The asymmetry is deliberate: a spurious task costs seconds to close; a
swallowed message about a child's team placement is unbounded.

**On the urgent exemption, precisely.** It does *not* make facts fresher or the
model more accurate. A stale or badly-worded fact is equally wrong for
`review` and `respond` mail, and that mail can matter. What the exemption does
is cap the blast radius of any gate error — model misjudgment, sloppy authoring,
or genuine decay — by keeping the highest-cost category out of reach. It is a
seatbelt, not a fix. It is nearly free because the mail this gate targets
classifies as `review`, so exempting `urgent` costs no coverage on the known
case. Reversible if it proves over-cautious.

### Staleness

A fact goes stale in four ways. Three are dangerous, one is not:

| Mode | Example | Direction |
|---|---|---|
| Role resumes, fact remains | Ben coaches again in 2027; "SafeSport cert expired, you cannot be on the field Saturday" is suppressed | **Dangerous** |
| Fact written too broadly | "SF Vikings mail is not actionable" eats Elijah's player invitation from `Lee@` | **Dangerous** (authoring error, not decay) |
| Role changes shape | Steps down as coach, becomes team manager — admin mail is signal again | **Dangerous** |
| Subject leaves entirely | Elijah leaves the team; player mail keeps creating tasks | Harmless — over-creates |

The dangerous direction is always the same: the fact asserts "not actionable"
and reality disagrees.

**Mode 1 is handled by scoping** (see "Facts carry their own scope"). A fact
that names the season it belongs to stops applying when that season ends, with
no human in the loop. This is the only mode with a clean structural fix, and it
is also the likeliest one, since roles are usually seasonal.

**Modes 2 and 3 have no structural fix and this spec does not claim one.**
Both are authoring errors rather than decay — mode 2 is wrong on the day it is
written, and mode 3 goes wrong *inside* the validity window, so no end date
catches it. Their backstops are:

1. **Fail-open.** A fact must clearly apply for the gate to act. Ambiguity
   creates the task.
2. **The suppression record.** "What has this fact eaten, and over what
   period" is a SQL query rather than an archaeological dig through logs — the
   only way an invisible failure becomes visible.
3. **Review at authoring time.** A fact lands via PR. The `Lee@sfvikings.com`
   distinction is exactly the thing a reviewer should be looking for, and this
   spec's sender table is the reason it is documented.

*Considered and rejected: a `Review by: YYYY-MM` field swept by the existing
daily `tasks-escalation` cron.* Scoping supersedes it for mode 1, and it does
nothing for modes 2 and 3 — it would add a scheduled job, a parser, and a
recurring notification to solve a problem the prose already solves.

### Recording suppressions

Log **and** database. No Asana artifact — a P3 reference row would re-create
the clutter this gate exists to remove.

New table in `repo/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS suppressed_emails (
    message_id  TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    importance  TEXT NOT NULL,
    subject     TEXT,
    title       TEXT,          -- the task title that would have been created
    reason      TEXT NOT NULL, -- the model's actionable_reason
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`schema.sql` is `CREATE TABLE IF NOT EXISTS` throughout and `scripts/migrate_db.py`
executes the whole file, so the migration is a re-run of the existing script.

Written through a new `repo/suppressions.py::insert`, following the existing
best-effort DB convention: a write failure logs at WARNING and does **not**
reverse the suppression. The decision was already made on evidence; undoing it
because Cloud SQL blipped would make task creation non-deterministic, which is
worse than a missing audit row. This is the one place the fail-open principle
deliberately does not apply, because the failure is in *recording* the
decision, not in *making* it.

Also emitted: counter `asana.tasks_suppressed` (exports as
`asana_tasks_suppressed`, matching the existing `asana_` series) with
attributes `category` and `importance`. The reason is deliberately not a metric
attribute — it is free text from a model and would blow up cardinality. It
lives in the log line and the table.

### Layering

Per the repo's layer rules:

| File | Change | Concern |
|---|---|---|
| `context/standing-context.md` | **new** | The facts. Not code. |
| `services/standing_context.py` | **new** | Loads and caches the file; `section(name)` accessor. Returns `""` on any read failure. |
| `services/policy.py` | `survives_context(summary, event)` added | Both gates in one file — CLAUDE.md: "Changing what becomes a task is a change HERE." |
| `services/email_summary.py` | prompt gains today's date + the `Roles` section; parses the two new fields | Transport only. The date is a precondition for scoped facts. |
| `services/deadline.py` | prompt gains the `Calendar` section | Return contract unchanged. |
| `models/events.py` | `EmailSummary` gains `actionable: bool = True`, `actionable_reason: str \| None = None` | Pure type; the `True` default *is* the fail-open default. |
| `handlers/task_create.py` | gate 2 call after `generate()`; suppression record | Orchestration only. |
| `repo/suppressions.py` | **new** | `insert()`. Takes an open connection. |
| `repo/schema.sql` | `suppressed_emails` table | — |
| `clients/otel.py` | `tasks_suppressed` counter | I/O only. |
| `.github/workflows/deploy.yml` | `context/**` in `paths:` | Without this, fact edits never deploy. |

No new client, no new dependency, no terraform change.

### Testing

- `tests/test_standing_context.py` — section extraction by heading; missing
  file returns `""`; missing section returns `""`; caching.
- `tests/test_policy.py` — `survives_context` truth table: actionable
  true / false / missing / false-without-reason / urgent-category exemption.
- `tests/test_email_summary.py` — the two new fields; malformed JSON asserts
  `actionable` defaults to `True`; empty `Roles` section leaves the prompt
  unchanged; today's date appears in the prompt whenever `Roles` is non-empty.
- `tests/test_deadline.py` — `Calendar` section reaches the prompt; empty
  section leaves the prompt unchanged.
- `tests/test_task_create.py` — a suppressed summary creates no Asana task,
  increments the counter, and writes the row; a DB failure on that write does
  not resurrect the task; an actionable summary is unaffected.

The Claude calls are mocked throughout via `monkeypatch.setattr(claude, ...)`,
as they are today.

Model judgment quality is verified manually against five real emails: the three
Micro Admin messages (must suppress) and the two `Lee@sfvikings.com` player
invitations (must survive). Scope expiry is verified by running the same three
Micro Admin emails against a frozen date after 2026-12-18 — they must **stop**
being suppressed, since the season the fact names has ended.

## Out of scope

- **Duplicate suppression.** The 7/31 email produced two near-identical tasks.
  This gate would not have caught that; it is a distinct problem.
- **The mail-history and existing-task checks** from `more_context_needed.md`.
  Both require new search calls and belong in their own spec.
- **The no-action-phrase veto** from `no_action_needed_example.md`. It shares
  gate 2's position and should reuse this plumbing, but it is a separate rule
  with its own evidence base. Deliberately sequenced after this.
- **An API endpoint over `suppressed_emails`.** The table is queryable via
  `psql` for now. Add a route when there is a second reader.
- **Runtime editing of facts.** They change on the order of months. A PR is the
  right ceremony and gives the change a review and a history.

## Open questions

1. **How large can the context file grow before per-call cost matters?**
   Sectioning bounds this — each consumer pays only for its own section. At
   present size it is negligible. Revisit if any single section passes roughly
   1k tokens, at which point relevance-filtering within a section becomes worth
   designing.
2. **Should `Calendar` be consulted for P2/P3 mail?** Deadline extraction runs
   only for P0/P1 today. Widening it is a separate cost/benefit question this
   spec does not reopen.

# Standing-Context Gate — Design

**Date:** 2026-08-18
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

## Design

### Where the gate runs

```
policy.warrants_task(event)          gate 1 — category, free, unchanged
email_summary.generate(event)        Haiku call — already happens
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

### Where the facts live

**`services/standing_context.py`, as a module-level string constant.**

This is forced by deployment, not preference. `terraform/cloud_functions.tf`
excludes `docs` from the source archive, so a facts file under `docs/` would
never reach the running function. The archive ships `services/`, and
`.github/workflows/deploy.yml` already triggers on `services/**`, so a fact
edit deploys itself with no new plumbing.

*Rejected: a new top-level `context/` directory.* It would ship (the archive's
`excludes` is a denylist), and it would keep the prose in a `.md` file. But
`deploy.yml`'s `paths:` filter is an allowlist — forgetting to add `context/**`
means fact edits merge to `main` and silently never deploy. A silent
no-op on the file whose entire purpose is to change behavior is the worst
available failure mode.

The content is prose, not config, because the consumer is a model and because
prose can express the `Lee@sfvikings.com` distinction that no schema can:

```python
STANDING_FACTS = """
## Roles

- **Assistant Coach, West Portal Proud Panthers (SF Microsoccer / SF Vikings)**
  — ENDED 2026-08-14. Ben resigned; Christy Dillon is handling the
  replacement. Elijah remains a player on the team.
  - Coach- and admin-directed mail — schedules to review, coach admin
    requirements, Micro Admin broadcasts — is NOT actionable.
  - Mail about Elijah as a player — invitations, rosters, parent logistics —
    IS actionable.
"""
```

Re-enabling next season is deleting that block. That directly answers the
requirement that suppression not be permanent: there is no expiry field to
maintain and no matcher to perform surgery on.

### The prompt extension

`services/email_summary.py` currently asks Haiku for
`{"key_points": [...], "title": "..."}`. When `STANDING_FACTS` is non-empty,
the prompt gains the facts block and two output fields:

```json
{
  "key_points": ["..."],
  "title": "Verb object",
  "actionable": true,
  "actionable_reason": "..."
}
```

Prompt language, in substance: *"Below are standing facts about the recipient.
Set `actionable` to false ONLY if one of these facts clearly makes this email
require nothing of him, and name the fact in `actionable_reason`. If no fact
clearly applies, set `actionable` to true. Do not reason beyond the facts
given."*

When `STANDING_FACTS` is empty the prompt is unchanged and the gate is skipped
entirely — zero cost, zero behavior change. That is the state the repo ships in
until the first fact is added.

### Fail-open contract

The gate may only ever **remove** a task it is confident about. Every other
path creates the task:

- Missing `actionable` field → create.
- Unparseable JSON, or the whole Haiku call failing → create (this already
  happens today; `generate()` swallows exceptions and returns an empty
  `EmailSummary`).
- `actionable: false` with no `actionable_reason` → create. A suppression that
  cannot name its justification is not trustworthy enough to act on.
- `event["category"] == "urgent"` → create, unconditionally. Gate 2 does not
  apply to urgent mail. A stale fact suppressing a P0 is the worst outcome
  this design can produce, and urgent is cheap to exempt. Reversible later if
  it proves over-cautious.

The asymmetry is deliberate: a spurious task costs seconds to close; a
swallowed message about a child's team placement is unbounded.

### Observability

Suppression must be visible, not silent.

- New counter `asana.tasks_suppressed` (exports as `asana_tasks_suppressed`,
  matching the existing `asana_` series), with attributes `category` and
  `importance`.
- Every suppression logs at INFO with `message_id`, the generated title, and
  `actionable_reason` — so `fetch-tasks-logs` shows what was dropped and why.

The reason is deliberately *not* a metric attribute: it is free text from a
model and would blow up cardinality.

### Layering

Per the repo's layer rules:

| File | Change | Concern |
|---|---|---|
| `services/standing_context.py` | **new** | Holds `STANDING_FACTS`. No logic. |
| `services/policy.py` | `survives_context(summary, event)` added | Both gates in one file — CLAUDE.md: "Changing what becomes a task is a change HERE." |
| `services/email_summary.py` | prompt + parse | Transport for the extra fields only. |
| `models/events.py` | `EmailSummary` gains `actionable: bool = True`, `actionable_reason: str \| None = None` | Pure type; default `True` is the fail-open default. |
| `handlers/task_create.py` | gate 2 call after `generate()` | Orchestration only. |
| `clients/otel.py` | `tasks_suppressed` counter | I/O only. |

No new client, no new dependency, no schema migration, no terraform change.

### Testing

- `tests/test_policy.py` — `survives_context` truth table: actionable true /
  false / missing / false-without-reason / urgent-category exemption.
- `tests/test_standing_context.py` — the empty-facts path skips the gate;
  a non-empty fixture reaches the prompt.
- `tests/test_email_summary.py` — extend for the two new fields, including a
  malformed-JSON case asserting `actionable` defaults to `True`.
- `tests/test_task_create.py` — a suppressed summary creates no Asana task and
  increments the counter; an actionable one is unaffected.

The Haiku call is mocked throughout, as it is today. Model judgment quality is
verified manually against the three known Microsoccer emails plus the two
`Lee@sfvikings.com` player invitations, which must survive.

## Out of scope

- **Duplicate suppression.** The 7/31 email produced two near-identical tasks.
  This gate would not have caught that; it is a distinct problem.
- **The mail-history and existing-task checks** from `more_context_needed.md`.
  Both require new search calls and belong in their own spec.
- **The no-action-phrase veto** from `no_action_needed_example.md`. It shares
  gate 2's position in the pipeline and should reuse this plumbing, but it is a
  separate rule with its own evidence base. Deliberately sequenced after this.
- **Runtime editing.** Facts change on the order of months. A PR is the right
  ceremony, and it gives the change a review and a history.

## Open questions

1. **Does `STANDING_FACTS` belong in the deadline call too?** `deadline.py`
   makes a Sonnet call for P0/P1 mail. A resigned role arguably affects
   deadline extraction as well. Deferred — no evidence yet that it misfires.
2. **How large can the facts grow before per-call token cost matters?** At one
   fact this is negligible. Revisit past roughly 1k tokens, at which point
   pre-filtering facts by relevance becomes worth designing.
3. **Should suppressed emails leave any trace in Asana?** Currently they leave
   only a log line and a metric. A P3 reference row is the alternative
   (considered and set aside during brainstorming as re-creating the clutter
   this removes), but if the log proves too invisible in practice, this is the
   natural second iteration.

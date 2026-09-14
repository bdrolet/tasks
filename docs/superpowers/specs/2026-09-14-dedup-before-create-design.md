# Deterministic dedup before task creation

**Date:** 2026-09-14
**Status:** designed, not implemented

## Problem

A single real-world matter can produce a task per notification email. Between
2026-09-08 and 2026-09-14, SFUSD attendance notices for three children created
**fourteen** near-duplicate tasks — "[P1] Clear Elijah's school absence" three
times, "[P1] Clear Amelia's school absence" three times, "[P0] Contact school
about Elijah's absence" three times, and so on — for what a person would call
one errand per school.

Three properties combine to produce that:

1. **The deterministic dedup path is unreachable for this mail.** Gate 1's
   `relate` verdict is the only thing that routes an email into
   `services/relating.py`, where a cosine floor and a confirm call live. But
   `services/screening.py:65` reserves `relate` for transactional confirmations
   and excludes "any report, assessment, evaluation, or progress update about a
   person — their health, care, education, development, or legal standing." An
   attendance notice is a report about a child's education, so it always takes
   the `task` branch and never meets a similarity floor.

2. **Gate 2's equivalent is a judgment call, not a rule.** `services/triage.py`
   offers the agent `search_tasks`/`get_task` and a `related_task_gid` output
   that `handlers/task_create.py:156` honours. Nothing requires the agent to
   search, and the prompt tells it to skip the tools "when the email is plainly
   a new request" (`services/triage.py:285`). Each dated notice reads as its
   own new request.

3. **The only hard idempotency key is per-message.** `clients/asana.py:123`
   stamps `external.gid = message_id` and treats Asana's "already assigned"
   error as a no-op. That defends against Pub/Sub redelivering the *same*
   event. Different emails about the same matter have different message ids.

Volume multiplies the effect: one absence arrives as a SchoolMessenger email
plus Google Voice voicemail notifications in more than one mailbox, and inbox's
ingestion dedup is keyed on the per-mailbox Graph item id, so copies of one
message are distinct messages to this service.

## Goals

- A second email about a matter that already has an open task becomes a comment
  on that task, not a new task.
- The decision is enforced by code with a measured threshold, not by an agent's
  discretion.
- Failure of any dependency degrades to today's behaviour: the task is created.
- The rule is tunable against recorded history before it ships.

## Non-goals

- Deduplicating copies of one message across mailboxes. That is inbox's
  ingestion concern, upstream of this service.
- Merging or closing tasks that already exist. This stage never edits, reopens,
  or completes anything.
- Replacing gate 2's `related_task_gid`. It stays as an additional path; this
  stage is the net underneath it.
- Changing gate 1's `relate` semantics or its population.

## Decisions

### D1 — A deterministic stage after gate 2, before enrichment

`handlers/task_create.py::handle` gains one branch: after the gate-2 block and
before the enrichment calls, `dedup.match(event)` runs; a match suppresses, a
non-match falls through to creation exactly as today.

After gate 2 rather than before it, so the deterministic check sits *underneath*
the agent rather than in front of it — the agent may still route an email to an
existing task on its own, and this stage catches what it misses, with the
research context of gate 2 already applied to the actionability question.
Before enrichment rather than after, so a duplicate also skips the Haiku summary
and the P0/P1 deadline extraction, not merely the Asana write.

Rejected: making gate 2's `search_tasks` call mandatory. That keeps the decision
inside a non-deterministic agent with no floor behind it, which is the mechanism
that failed.

### D2 — "Already tracked" means the same ongoing matter, on any date

A notice about a later date of a matter that already has an open task folds into
that task. Three absence dates for one child are one task, and the second and
third dates arrive as comments on it.

Rejected: folding only exact repeats of the same underlying event (same student,
same date). That would have reduced fourteen tasks to three rather than to two,
and three separate "clear this absence" tasks is still not what the errand is.

### D3 — A cosine floor gates a single Haiku confirm; every failure fails open

The score decides who is considered; one Haiku call decides whether it is the
same matter. This mirrors `services/relating.py` and is the same cost shape: no
call at all below the floor, one cheap call above it.

Score-only was rejected. A wrong fold here is worse than a wrong comment: the
task never exists, and nothing in the product shows an absence of a task. The
confirm is a second opinion whose default answer is "no".

Fail-open is absolute. Vertex down, Postgres down, Haiku down, malformed JSON,
a gid outside the candidate set, a gid Asana will not return — every one of them
returns no match, and the task is created. Worst-case behaviour is today's
behaviour.

### D4 — Only open tasks are candidates

`repo_index.semantic_candidates(completed=False)`, as `relating.py` already
does. Once a task is completed, the next email about that matter creates a fresh
task. This is the self-healing property: a fold that turns out to be wrong
cannot persist past the point where the task is closed, and a recurring matter
resumes cleanly after each round is finished.

### D5 — A fold writes one comment per folded email and one suppression row

The fold reuses `handlers/task_create.py::_suppress` with `source="dup"`. No new
table: `suppressed_emails` already records `related_task_gid`, `reason`,
`evidence` and is idempotent on `message_id`, and `_suppress`'s existing guard
(`repo_suppressions.exists`) already prevents a second comment on Pub/Sub
redelivery.

`_suppress` currently chooses between two comment leads ("Related email:" and
the resolves wording). It gains an optional `lead` parameter so dedup can say
"Another notice about this:". No other change to its contract.

Rejected: suppressing the comment for near-identical repeats. It requires
tracking what has already been commented, and the noise it saves is bounded and
visible, whereas a missing comment is not.

### D6 — A separate module, with the shared parts extracted

`services/dedup.py` is new and separate from `services/relating.py`. The two
stages ask different questions — relate asks whether an email *reports on* a
task, dedup asks whether an email *is* a request a task already exists for — and
relate's `resolves`/`verb_check` machinery is meaningless here. Separate modules
keep the prompts, the floors and the metrics independently tunable.

What is genuinely shared is extracted rather than copied:

- `services/matching.py::candidates(event, *, limit)` — the embed-and-query
  helper currently at `services/relating.py:93`. Both stages call it.
- `services/matching.py::verify_gid(gid, allowed)` — "the gid must be one the
  model was shown, and Asana must return it, else treat it as no match."
  Currently duplicated at `services/relating.py:148-152` and
  `services/triage.py:332`; the screening design already called for lifting it.

Rejected: a `mode=` flag on `relating.match()`. The prompts diverge, the floors
diverge, and branching inside a prompt that already carries fifteen rules makes
both harder to tune.

### D7 — `DUP_FLOOR` ships only with a measured run behind it

`SIMILARITY_FLOOR = 0.65` in `relating.py` was set from a replay of 1,266
historical emails, and its comment records what the bands contained. `DUP_FLOOR`
gets the same treatment. It starts at a provisional **0.70** — above relate's
floor, because the cost of a wrong answer is higher — and the implementation is
not finished until `scripts/backtest_screening.py` has been extended, run, and
the chosen number justified in a comment the way relate's is.

### D8 — The stage never edits the matched task

No due-date bump, no reopen, no completion, no tag change. It posts a comment
and records a row. The task's content remains whatever the first email and its
owner made it. This matches `relating.py`'s contract and keeps the failure mode
legible: the worst a fold can do is put a comment on a task and leave an errand
untracked, both of which are visible on the task itself.

## Architecture

```
email_classified
      │
      ▼
screening.screen                      gate 1  (unchanged)
      ├── drop   → _suppress(source="screen")
      ├── relate → relating.match → _suppress(source="relate")
      │
      ▼ task
triage.decide                         gate 2  (unchanged)
      ├── not actionable / related_task_gid → _suppress(source="agent")
      │
      ▼ actionable, unrelated
dedup.match                           NEW
      ├── task_gid → _suppress(source="dup", lead="Another notice about this:")
      │
      ▼ no match
enrichment → asana.create_task → tasks row → section → task_index.refresh
```

### `services/dedup.py` (new)

```python
DUP_FLOOR = 0.70          # provisional; see D7
CANDIDATES = 3
BODY_CAP = 2000

def match(event: EmailClassifiedEvent, *, rows: list[dict] | None = None) -> Match
```

Returns `models.events.Match`, reused as-is; `resolves` is always false on this
path. The `rows=` seam exists for the same reason it exists on
`relating.match()` — so the offline replay exercises this exact function rather
than a copy of its control flow.

Flow: `matching.candidates(event, limit=CANDIDATES)` → if empty or
`rows[0]["score"] < DUP_FLOOR`, return no match → one `claude.classify` call
with the candidates rendered as relate renders them → `matching.verify_gid` →
`Match`.

The prompt's shape, and where it differs from relate's:

- The question is whether an **open task already covers this same matter**, such
  that creating another would be a duplicate errand.
- A later notification about a different date, period, or occurrence of an
  ongoing matter **is** the same matter. The recurring-notice case is stated
  explicitly, because it is the case that motivated the stage.
- Different matters that merely share a sender, a template, or a subject are
  not. Two children at one school, two invoices from one vendor, two
  appointments with one clinic are separate matters.
- `null` is the expected answer and costs nothing; a wrong gid means the errand
  is never tracked.
- The gid must be copied verbatim from the candidates shown.
- `reason` is published verbatim as the Asana comment: one short sentence naming
  what makes this email the same matter as the task.

### `services/matching.py` (new)

Holds `candidates()` and `verify_gid()` as described in D6. `relating.py` keeps
its own `BODY_CAP`/`NOTES_CAP`/prompt and calls the shared helpers;
`build_user_message` stays in `relating.py` and dedup gets its own, since the
framing differs.

### `handlers/task_create.py`

One new branch (D1) and the `lead` parameter on `_suppress` (D5). Nothing else
moves.

## Observability

- **`asana.tasks_deduped{matched}`** — new counter, one per dedup evaluation,
  `matched` true or false. The ratio against `asana.tasks_created` is the
  headline number.
- **`asana.tasks_suppressed{source="dup", attached="true"}`** — falls out of the
  existing counter, since `source` is already an attribute.
- **Logs** — `dedup below floor best=%s message_id=%s` and
  `dedup matched=%s message_id=%s reason=%s`, mirroring relate's lines, so a
  fold can be reconstructed from logs alone.

A fold is also visible without any telemetry: the comment is on the task, and
the row is in `suppressed_emails` with `source='dup'`.

## Testing

Unit tests drive `match()` with injected `rows`, so no test needs Vertex or
Postgres:

| Case | Expected |
|---|---|
| No candidates | no match → task created |
| Best score below `DUP_FLOOR` | no match, no Haiku call |
| Above floor, confirm returns null | no match |
| Above floor, confirm returns a candidate gid | fold |
| Confirm returns a gid outside the candidate set | no match |
| Confirm returns a gid Asana will not fetch | no match |
| `candidates()` raises | no match |
| `claude.classify` raises or returns malformed JSON | no match |

Handler-level tests cover the branch itself: a fold calls `_suppress` with
`source="dup"` and never calls `asana.create_task`, enrichment, or
`deadline.extract_deadline`; a non-match reaches creation unchanged.

**Offline replay.** `scripts/backtest_screening.py` gains dedup columns for
`task`-verdict rows — best neighbour score, candidate gids, whether the confirm
folded, and its reason — so `DUP_FLOOR` is chosen from the score distribution of
real history, and the attendance-notice sequence is available as a known-positive
case. This is the ship gate named in D7.

## Files

| File | Change |
|---|---|
| `services/dedup.py` | New. Floor, prompt, schema, `match()`. |
| `services/matching.py` | New. `candidates()`, `verify_gid()`. |
| `services/relating.py` | Calls the shared helpers; loses its private copies. |
| `services/triage.py` | Uses `matching.verify_gid` in place of its inline check. |
| `handlers/task_create.py` | Dedup branch; `lead` parameter on `_suppress`. |
| `clients/otel.py` | `tasks_deduped` counter. |
| `scripts/backtest_screening.py` | Dedup columns and summary. |
| `tests/` | As above. |
| `CLAUDE.md` | Task policy section gains the dedup stage. |

No database migration: `suppressed_emails` and `task_index` already hold
everything this needs. No Terraform change, no new secret, no new environment
variable.

## Rollout

1. Implement behind no flag — fail-open is the safety property, and a flag would
   add a second way to be wrong.
2. Extend and run the backtest; fix `DUP_FLOOR` from the measured distribution
   and record the bands in the constant's comment (D7).
3. Deploy with the normal Cloud Function deploy.
4. Watch `asana.tasks_deduped{matched="true"}` for a week against
   `asana.tasks_created`. A fold rate materially above the duplicate rate the
   backtest predicted means the floor is too low.
5. Spot-check folded tasks: every fold is a comment on a real task, so reading
   the comments is the audit.

## Risks

**A wrong fold means an errand is never tracked.** This is the one that matters,
and it is strictly worse than relate's failure mode, which only ever adds a
false comment to a real task. Mitigations: the floor, the candidate-set
restriction, a confirm whose default is null, fail-open on every dependency, and
D4's rule that closing a task ends any fold into it. Residual risk is accepted:
the comment lands on a task that is open and in front of the owner.

**Two matters that look alike get merged.** Two children at one school, two
invoices from one vendor. The prompt addresses this directly and the backtest is
where it gets measured; the same-sender-different-subject case is exactly what
the corpus contains.

**Corpus staleness.** A task created seconds earlier may not be indexed when the
next copy of an email arrives. `task_index.refresh` runs inline at creation
(`handlers/task_create.py:236`), so the window is small, but near-simultaneous
deliveries of one notice can still both create. The `external.gid` guard does
not help across distinct message ids. Accepted: the second task is a duplicate
of the kind this stage otherwise removes, at much lower volume.

**Added latency and cost on the create path.** One embed plus, above the floor,
one Haiku call per task-verdict email. Small next to the Sonnet agent that has
already run by this point, and duplicates now skip enrichment entirely, which
partly pays for it.

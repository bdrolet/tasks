# Strategy layer — goals, areas, necessity and tripwires for the prioritizer

**Date:** 2026-10-09
**Status:** designed, not implemented
**Builds on:** `2026-09-23-next-prioritizer-design.md` (the WSJF scorer this
extends), `2026-08-18-standing-context-gate-design.md` (the private declared
facts this reads the same way). Research notes behind the choices:
`docs/superpowers/research/2026-10-09-strategy-frameworks.md`.

## Problem

The prioritizer answers "what is most costly to delay". It cannot answer
"does any goal need this at all", "what is the next necessary step toward
landing a client", "what can I do now to protect that goal", or "is the plan
still working". A task gets a `[PX]` prefix and a date and from then on ranks
like real work, whether or not it moves anything: a check with the accountant
about a question whose answer would not change what gets filed ranks beside
the proposal that would bring in revenue.

Ben has written goals down at least three times before (a runway plan in
December 2022, a horizons-style goals stack in 2023, a priorities note in
2025). Each lived in a document nothing read, and each went stale within
months. The research survey found the same failure in every commercial tool:
goals become a parallel system nobody reviews, and the link runs goal → work,
never work → goal, so a task cannot say what it serves.

## Goals

- Every task carries **what it serves** and **in what role**, set at creation
  and visible on the card; nothing enters the ranked set unattached without
  being flagged. For a task born from email, "at creation" is literal: gate 2
  judges necessity in the same call that decides whether the email is
  actionable, and the task is created with its tags (D14).
- Ranking prefers work that is on a goal's path or protects a goal or an
  area's standard, over work that merely supports one, and sinks work that
  serves nothing.
- A **grooming** list shows the judgments the model was unsure of; a
  **stop-doing** list shows the tasks it is confident serve nothing, each
  with a prompt to remove or attach.
- Each **outcome goal** shows its current next step, its lead and lag
  measures against their thresholds, and whether a pre-registered
  **tripwire** has fired. Each **area** shows whether it is above or below
  its standard.
- Strategy is **written prose Ben edits**, in a form a model can reason
  from (diagnosis, guiding policy, path with assumptions), and the system
  flags it when it goes stale.
- The model's necessity judgment is **measured** against Ben's corrections,
  the way point estimates already are, and the amount of trust the scorer
  places in it is a config setting that moves only when that measurement
  justifies it.

## Non-goals

- **No new strategy from the model.** It judges tasks against the written
  strategy and enforces coherence; it does not write or rewrite goals.
- **No Asana Goals.** Asana's goal object cannot be linked from the task
  side; the display half buys nothing. Linkage is tags on the task.
- **No automatic deletion.** The stop-doing list prompts; Ben removes.
- **No revenue integration.** Lag measures are reported by hand.
- **No change to gate 1.** Screening stays a cheap Haiku call over every
  email with no strategy in its prompt. Gate 2 does read the strategy (D14);
  it is already the judgment step and already reads the declared facts.
- **No multi-user.** One strategy document, one person.

## Vocabulary

| Term | Meaning |
|---|---|
| **Outcome goal** | An end state with a clock: "≥ $15k/month from consulting before runway ends". Has a path, assumptions, lead and lag measures, tripwires. |
| **Area** | A standard to keep, with no end state: finances, home, health, social, family, job search. Has a standard and a below-the-line signal. |
| **Role** | How a task relates to a goal or area: `path` (a step on an outcome goal's path), `derisk` (protects a goal or standard from a significant downside), `support` (helps without being necessary). |
| **Lead measure** | A controllable, predictive count: conversations held per week. Computed from completed tasks. |
| **Lag measure** | The outcome: revenue per month. Reported by hand. |
| **Tripwire** | A pre-registered state-and-date: "0 signed clients by 2026-12-31 → revisit niche and offer". Evaluated daily; fires as a task. |
| **Below the line** | An area's slipping signal: "any overdue bill". Boosts that area's tasks while true. |

## Decisions

### D1 — Strategy is a section of the private standing context

The document is `strategy.md` in the private `bdrolet/context` repo, beside
`roles.md` and `calendar.md`. That repo's CI already concatenates per-domain
files into the `standing-context` secret, so the file becomes a `## Strategy`
section with no change there. This service reads it through
`services/standing_context.section("Strategy")`. Editing strategy is a PR in
that repo and takes effect on the next cold start; no deploy here.

Why not this repo: a diagnosis of Ben's runway and his children's needs is
personal and this repo is public — the same reason the facts live there.
Why not the database: the path to an outcome goal is a hypothesis that gets
rewritten as evidence arrives, and prose is the cheapest thing to rewrite.

**Infrastructure:** today only the events CF mounts the secret. The
`tasks-prioritize` CF gets the same mount and `STANDING_CONTEXT_PATH`
(`terraform/cloud_functions.tf`), since enrichment is where the judgment
runs. The tasks-api does not read the document; it reads materialised rows.

### D2 — One `###` block per goal or area: a parsed header, then prose

Under `## Strategy`, each goal or area is a `### <id>` block. The id is the
heading text: lowercase, `[a-z0-9-]+`, and it is what `serves:<id>` tags
name. The block opens with a header of `- key: value` lines, which
`services/strategy.py` parses, and continues in prose, which only the model
reads. The header grammar:

```
- kind: outcome | area                       required
- weight: <float>                            default 1.0; multiplies necessity
- horizon: YYYY-MM-DD                        outcome only; the runway date
- lag: <text> <op> <number> per <period>     outcome only; reported by hand
- lead: <tag> <op> <number> per <period>     outcome only; repeatable
- tripwire: <text> <op> <number> by YYYY-MM-DD -> <action text>
                                             outcome only; repeatable
- standard: <text>                           area only; prose for the model
- below-the-line: <signal>[; <signal>]       area only; see D9
- review: weekly | monthly                   default weekly
```

`<op>` is one of `>= <= = < >`. `<period>` is `day | week | month`. The
`lead` tag is a plain Asana tag name (`conversation`, `proposal`); a
completed task counts toward the lead when it carries that tag **and** a
`serves:<id>` tag for this goal. `tripwire` text before the operator is either
a lead tag name (counted the same way, over the whole window since the
document's `last reviewed` date) or the literal `lag`.

The section also carries one top-level line, `- last reviewed: YYYY-MM-DD`,
before the first `###`.

Prose conventions (Rumelt's kernel, which the research found to be the form
a model enforces best): for an outcome goal, **Diagnosis**, **Guiding
policy** (including what is explicitly *not* being done), **Path** (ordered
preconditions, each with the assumption it rests on and an evidence status —
untested, weak, confirmed), **Derisks** (pre-mortem failure modes and the
task that would catch each early). For an area, **Standard** and **Not
doing**. These are conventions the prompt describes; the parser does not
require them.

The full example lives in `context/standing-context.example.md` in this
repo, under a `## Strategy` section, with placeholder content.

### D3 — Parsing is pure and lenient; no document means no feature

`services/strategy.py::parse(text) -> Strategy` is I/O-free and returns
`Strategy(goals: tuple[Goal, ...], last_reviewed: date | None,
findings: tuple[str, ...])`. A block with no `kind`, an unknown `kind`, a
header line that does not parse, or a duplicate id is **skipped with a
warning** and recorded in `findings`; the rest of the document loads. A
missing or empty section yields `Strategy((), None, ())`, and every
consumer treats that as "necessity is neutral" — the scorer's new term is a
constant and ranking is unchanged from today.

`findings` also carries review-level checks, which are warnings for the
weekly review, never parse failures: an outcome goal with no tripwire; an
outcome goal with no `lead`; `last reviewed` missing or older than
`config.strategy.stale_after_days` (90).

### D4 — The existing enrichment call judges necessity

`services/enrichment.py`'s single schema-constrained call (prioritizer D5)
gains the strategy and three output fields. No second model call per task.

**Prompt additions.** System: what goals, areas and the three roles mean;
that `path` means "a precondition on the goal's written path, or the
obvious next step toward one"; that `derisk` means "its absence puts the
outcome or the standard at significant risk"; that `support` means "helps
but the goal is reachable without it"; that an empty `serves` with high
confidence is a real and useful answer, not a failure. User: the full
`## Strategy` section text, after the task content.

**Schema additions** (strict, alongside the existing fields):

```json
{
  "serves": [
    {"goal": "<id>", "role": "path" | "derisk" | "support",
     "confidence": "low" | "medium" | "high"}
  ],
  "necessity_confidence": "low" | "medium" | "high",
  "necessity_reason": string
}
```

`serves` may be empty. `necessity_confidence` is the model's confidence in
the `serves` list as a whole (including "nothing"). A `goal` that names no
id in the loaded strategy is dropped at parse time with a warning.

**Cache key.** `content_hash` (D5) becomes
`sha256(name + "\n" + notes + "\n" + comments + "\n" + strategy_hash)`
where `strategy_hash` is `sha256` of the `## Strategy` section text. A
strategy edit therefore re-judges every task once on the next gather or heal
— ~100 calls, about a dollar at the current volume — which is correct: a new
guiding policy should re-sort the list. The hash is stored on
`task_enrichment` so `calibrate` can group judgments by strategy version.

**Precedence is unchanged:** tag > override > model > default, per field.
Two new tag families, `serves:<id>` (repeatable) and `role:<path|derisk|support>`
(one per task; it applies to every `serves:` tag on that task), and two new
override fields, `serves` (list of ids) and `role`. Ben's tag always wins.

### D5 — Draft write-back, once, under the same guard as story points

After enrichment, when the task carries no `serves:` tag **and**
`task_facts.serves_estimated IS NULL`:

| Judgment | Action |
|---|---|
| `serves` non-empty, `necessity_confidence` medium or high | write `serves:<id>` for each entry and one `role:<role>` tag (the highest role present, path > derisk > support); set `serves_estimated` to the JSON written; post the comment `Attached to {ids} as {role} — adjust the tags if wrong.` |
| `serves` non-empty, confidence low | write nothing; `serves_estimated = '{"grooming": true}'`; the task appears in the grooming list |
| `serves` empty, confidence medium or high | write nothing; `serves_estimated = '{"none": true}'`; the task appears in the stop-doing list |
| `serves` empty, confidence low | write nothing; `serves_estimated = '{"grooming": true}'` |

The guard is the conditional `UPDATE … WHERE serves_estimated IS NULL` whose
rowcount decides who writes, exactly as `points_estimated` (prioritizer D6),
so redelivery cannot write twice. Never written again for that task: a later
tag that differs from `serves_estimated` means Ben corrected it, and both
are kept for `calibrate`. The service's own comment is excluded from the
content hash via `is_estimate_comment`, extended to recognise it.

Ben's "ask at creation, always" is met at every creation path: the email
pipeline and the API both already call `task_index.refresh` and publish
`task_changed` after creation, which is what triggers this. The
`task-builder` agent's rule additionally sets `serves:` and `role:` itself
from the strategy section (a prompt-level rule in `.claude/agents/`, so the
draft never runs for a task the agent built).

### D6 — Scoring gains one cost-of-delay term, `necessity`, and one bucket

`services/prioritize.py` stays pure; `score_set` takes a `Strategy`.

**Effective necessity** per task, from the effective `serves`/`role`
(tag > override > model):

```
role factor:  path 1.0 | derisk 0.9 | support 0.5          (config)
N = max over serves of (goal.weight × role factor)
N = config.necessity.unattached  (0.2)  when serves is empty and
                                        necessity_confidence is low
N = config.necessity.unattached         when unenriched, or when no strategy
                                        is loaded (then every task gets the
                                        same N, so ranking is unchanged)
```

**Bucket.** When `serves` is empty with medium/high confidence and the mode
is `suppress`, the task's bucket is `stop_doing` — excluded from `next`
like `excluded:blocked`, listed by `GET /review`, and a pin overrides it the
way a pin overrides blocked. In any other mode the task stays in `next`.

**Mode** (`config.necessity.mode`) sequences how much the scorer trusts the
judgment:

| mode | effect |
|---|---|
| `flag` | N is recorded in `components` and the grooming/stop-doing lists are populated; the `necessity` weight is forced to 0 so ranking is unchanged. **Ship here.** |
| `demote` | the `necessity` weight applies; uncertain "none" sinks via `unattached`. |
| `suppress` | as `demote`, plus the `stop_doing` bucket. |

Moving from `flag` is a config edit, made when `GET /calibrate` shows the
agreement rate (D10) that justifies it. The threshold is a judgment, not a
number in this spec; the point is that the number exists before the trust
does.

**Weights.** `[weights]` gains `necessity`; the block still sums to 1.0. The
initial values rebalance to `priority 0.20, urgency 0.25, impact 0.10,
unblock 0.10, aging 0.05, category 0.10, necessity 0.20`. In `flag` mode
the scorer renormalises the remaining weights so today's ranking is
reproduced exactly.

**Goal horizon as soft due date.** For a task whose effective role is
`path` or `derisk` on an outcome goal with a `horizon`, and which has no
hard date and no inferred date, the goal's `horizon` becomes the effective
due date with `due_source = "goal_horizon"` and the existing
`soft_cap_horizon` cap. It replaces the priority horizon
(`created_at + horizon[priority]`) only when earlier. Goal work gets a clock
without an invented deadline, and the no-invented-due-dates rule is kept:
nothing is written to Asana.

**Below-the-line boost.** While an area's signal is true (D9), every task
serving that area has its cost of delay multiplied by
`config.necessity.below_the_line_boost` (1.3). The slipping standard climbs
without Ben touching anything.

### D7 — Measures and tripwires are evaluated on the daily tick

`handlers/prioritize.py::handle_day_changed` gains a step after the heal
and before the rescore: `services/goal_state.py::evaluate(strategy, facts,
reports, today) -> list[GoalState]` (pure), written to `goal_state`.

**Lead rate.** For `lead: conversation >= 3 per week`: the count of
`task_facts` rows that are completed, carry tag `conversation` and tag
`serves:<id>`, with `completed_at` in the trailing window (7 / 30 days;
`day` is the trailing 1). The rate and the threshold are both stored, with
`met: bool`.

**Lag.** The most recent `goal_reports` row for the goal; `met` against the
`lag` line's threshold; `null` when never reported.

**Tripwire.** For each `tripwire:` line: evaluated only when `today >= by`;
the measured value is the lead-style count since `last reviewed` (or the
latest lag report when the text is `lag`); `fired` when the comparison
holds. A tripwire has an ordinal (its position in the block, 1-based) so
its identity survives re-ordering of prose but not of tripwire lines —
acceptable, because a tripwire edit is a strategy change.

**Next step.** The highest-scored `next`-bucket task whose effective role is
`path` for the goal, after the rescore; `null` means **stalled** and the
review says so.

**Below the line** for an area: any configured signal true (D9).

### D8 — A fired tripwire becomes a task

When `evaluate` reports a tripwire `fired` that `goal_state` did not record
as fired yesterday, the handler creates one Asana task in the default
project: name `[P1] {action text}` (e.g. `[P1] Revisit consulting niche and
offer`), notes carrying the tripwire line, the measured value, and a link to
the review; tags `serves:<id>`, `role:derisk`, `tripwire`; section Review;
`external.gid = tripwire:{goal-id}:{ordinal}:{by-date}`. The external id is
the idempotency guard against redelivery and against the tripwire staying
true on later days — the same pattern as `recur:{gid}`. Creation goes
through the existing `create_task` path so it publishes `task_changed` and
is judged and ranked like anything else.

Why a task and not a notification: the ranked list is the one place Ben
reliably looks, and the research's finding is that strategy-change signals
are missed precisely because they live somewhere else.

### D9 — Below-the-line signals are a small closed grammar

`below-the-line:` accepts a `;`-separated list of signals, each one of:

| Signal | True when |
|---|---|
| `overdue` | any open task serving this area has a hard due date before today |
| `overdue:<tag>` | as above, restricted to tasks carrying `<tag>` |
| `undated:<tag> after YYYY-MM-DD` | today is after the date and an open task serving this area carries `<tag>` with no hard due date |
| `lead <tag> < <n> per <period>` | the area's lead-style count is below `n` |
| `stale > <n> days` | no task serving this area has been completed in `n` days |

Anything else is a parse warning and the signal is ignored. The grammar is
closed on purpose: a signal must be computable from `task_facts` alone, so
the daily tick needs neither Asana nor a model.

### D10 — Necessity judgments are calibrated like points

`GET /calibrate` gains a `necessity` section: for every task with a
non-null `serves_estimated`, compare the model's draft against the task's
current effective `serves`/`role` (which, if different, Ben changed).
Report agreement rate by `necessity_confidence` band, by `strategy_hash`
and by source (`gate2` from `tasks.serves_estimated`, `enrichment` from
`task_facts.serves_estimated`), plus the count of grooming-list tasks Ben
attached versus removed, and the gate-2 / enrichment agreement rate where
both judged the same task (D14). This is the number that moves `mode` (D6).

`task_override_events` (audit-trail spec) already records override writes;
tag edits arrive as `task_changed` and are visible as a changed effective
value against `serves_estimated`, so no new audit table is needed.

### D11 — The weekly review is a task, and an endpoint

`GET /review` on tasks-api returns, from `goal_state`, `task_scores`,
`task_facts` and the loaded `findings`:

```json
{
  "reviewed_at": "...", "strategy_last_reviewed": "2026-10-09",
  "findings": ["consulting: no tripwire", "strategy last reviewed 112 days ago"],
  "goals": [{
    "id": "consulting", "kind": "outcome", "next_step": {gid, name} | null,
    "stalled": false,
    "leads": [{"tag": "conversation", "window": "week", "value": 1, "threshold": 3, "met": false}],
    "lag": {"value": 0, "threshold": 15000, "reported_at": "..."} | null,
    "tripwires": [{"ordinal": 1, "text": "...", "by": "2026-12-31", "value": 0, "fired": false}],
    "diagnosis": "lead weak" | "lead strong, lag flat" | "on track" | "insufficient data"
  }, {
    "id": "finances", "kind": "area", "below_the_line": true,
    "signals": [{"signal": "overdue", "true": true, "tasks": [gid, ...]}],
    "next_step": {...} | null
  }],
  "grooming": [{gid, name, serves_suggested, confidence, reason}],
  "stop_doing": {
    "tasks": [{gid, name, reason}],
    "suppressed_emails": [{message_id, subject, sender, web_link, reason, created_at}]
  }
}
```

`diagnosis` is the lead/lag rule from the research: leads met and lag not
met for two consecutive lag periods → "lead strong, lag flat" (the theory is
wrong — change strategy); leads not met → "lead weak" (execution, not
strategy); fewer than two lag reports → "insufficient data".

A Cloud Scheduler job `tasks-weekly-review` (`0 7 * * 1` America/New_York)
posts `POST <webhook-url>/review` (escalate bearer), which renders the
response as a comment on a standing task `[P2] Weekly strategy review`
(`external.gid = review:weekly`, created if missing, never completed by the
service). The cadence is a thing in the list, not a habit.

### D12 — Lag values are reported by hand

`POST /goals/{id}/reports` `{value, period_start?}` writes `goal_reports`.
The `task-next` agent gains `report <goal> <value>` and `review` (prints
`GET /review`). No revenue integration; the number comes from Ben monthly.

### D13 — The subscriber requires the database, as before

Prioritizer D7 holds: a DB or Asana failure raises so Pub/Sub redelivers.
An unreadable strategy section is **not** a failure: `Strategy((), None,
())` loads, necessity is neutral, and a warning is logged with the path.
A failed model call leaves `serves` unenriched and neutral, as the existing
fields are.

### D14 — Gate 2 reads the strategy and tags at creation

`services/triage.py::decide` already reads the `Roles` section of the
declared facts and decides whether an email still requires anything. It
gains the `## Strategy` section in the same system prompt and the same three
output fields as enrichment (D4): `serves`, `necessity_confidence`,
`necessity_reason`. One call, no second agent run.

`actionable` is decided as today — strategy never makes a non-actionable
email actionable. Strategy adds a second axis to an actionable email:

| Gate-2 judgment | `flag` / `demote` | `suppress` |
|---|---|---|
| `serves` non-empty, confidence medium/high | create with `serves:`/`role:` tags | same |
| `serves` non-empty, confidence low | create without tags; enrichment judges again (D5) → grooming | same |
| `serves` empty, confidence medium/high | create without tags; enrichment judges again → stop-doing | **suppress**: `suppressed_emails` row, `source = "necessity"`, reason = `necessity_reason`, no task |
| `serves` empty, confidence low | create without tags → grooming | same |

The `mode` setting is the one in `[necessity]` (D6): the gate suppresses on
necessity grounds only once the scorer is trusted to, and the same calibrate
number governs both. Suppression here is the only place in the design where
a necessity judgment acts without Ben seeing a card, which is why it waits
for `suppress` mode and why every such row is listed by `GET /review` under
`stop_doing.suppressed_emails` with the email's `web_link`, so a wrong call
can be reversed by building the task from the email.

The gate-2 draft is recorded on the pipeline's `tasks` row
(`tasks.serves_estimated`, same JSON shape as `task_facts.serves_estimated`)
so `calibrate` (D10) can score it against Ben's final tags, and against
enrichment's own judgment of the same task when both ran — disagreement
between the two gates is itself a signal about the prompt.

Gate 1 is untouched: it runs over every email inbox publishes, the strategy
section is long, and screening's job is "is there anything here", not
"does it matter". Fail-open holds: an unreadable strategy section leaves
gate 2 exactly as it is today, and a gate-2 failure already degrades to the
category rule.

## Components

| File | Change |
|---|---|
| `services/strategy.py` | **new, pure** — `parse`, `Goal`, `Strategy`, `strategy_hash` |
| `services/goal_state.py` | **new, pure** — `evaluate`, lead/lag/tripwire/below-the-line logic |
| `services/enrichment.py` | prompt + schema additions (D4); `is_estimate_comment` recognises the attach comment |
| `services/triage.py` | strategy in the system prompt; `serves`/`necessity_confidence`/`necessity_reason` in the schema (D14) |
| `handlers/task_create.py` | tag at creation; necessity suppression in `suppress` mode; record the draft on the `tasks` row (D14) |
| `repo/tasks.py` | `serves_estimated` on the pipeline row |
| `services/prioritize.py` | `necessity` term, `stop_doing` bucket, goal-horizon due, below-the-line boost, `flag` renormalisation (D6) |
| `services/prioritize_config.py` | `[necessity]`, `[strategy]`, `weights.necessity` |
| `handlers/prioritize.py` | draft write-back (D5); `evaluate` + tripwire tasks on `day_changed` (D7, D8) |
| `handlers/weekly_review.py` | **new** — renders `GET /review` as the standing-task comment (D11) |
| `repo/prioritize.py` | `serves_estimated` claim; `strategy_hash` on enrichment |
| `repo/goals.py` | **new** — `goal_state`, `goal_reports` |
| `api/routers/next.py` | `/calibrate` necessity section (D10) |
| `api/routers/review.py` | **new** — `GET /review`, `POST /goals/{id}/reports` |
| `main.py` | `review` route on the webhook CF |
| `terraform/cloud_functions.tf` | mount `standing-context` on `tasks-prioritize`; scheduler `tasks-weekly-review` |
| `context/standing-context.example.md` | `## Strategy` example |
| `.claude/agents/task-next.md`, `task-builder.md`, skill `prioritizing-tasks` | `review`, `report`; `serves:`/`role:` tagging rule |
| `config/prioritize.toml` | see Config |

## Data model

```sql
ALTER TABLE task_facts ADD COLUMN serves_estimated JSONB;   -- NULL = never judged for write-back (D5)
ALTER TABLE tasks ADD COLUMN serves_estimated JSONB;        -- gate-2 draft at creation (D14)
ALTER TABLE task_enrichment ADD COLUMN strategy_hash TEXT;  -- D4

CREATE TABLE IF NOT EXISTS goal_reports (
    id            BIGSERIAL PRIMARY KEY,
    goal_id       TEXT NOT NULL,
    value         DOUBLE PRECISION NOT NULL,
    period_start  DATE,
    reported_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS goal_reports_goal_idx ON goal_reports (goal_id, reported_at DESC);

CREATE TABLE IF NOT EXISTS goal_state (
    goal_id       TEXT NOT NULL,
    day           DATE NOT NULL,
    kind          TEXT NOT NULL,            -- outcome | area
    state         JSONB NOT NULL,           -- leads, lag, tripwires, below_the_line, signals, next_step, diagnosis
    strategy_hash TEXT NOT NULL,
    PRIMARY KEY (goal_id, day)
);
```

`suppressed_emails.source` gains the value `necessity`. `task_scores.bucket` gains the value `stop_doing`. `components` gains
`necessity`, `serves` (effective), `role` (effective), `necessity_source`
(`tag | override | model | default`), `grooming: bool`,
`below_the_line: bool`, and `due_source` gains the value `goal_horizon`.

## Config (`config/prioritize.toml`)

```toml
[weights]                     # sum to 1.0
priority = 0.20
urgency = 0.25
impact = 0.10
unblock = 0.10
aging = 0.05
category = 0.10
necessity = 0.20

[necessity]
mode = "flag"                 # flag | demote | suppress  (D6)
path = 1.0
derisk = 0.9
support = 0.5
unattached = 0.2              # serves empty with low confidence; unenriched; no strategy
below_the_line_boost = 1.3    # multiplier on cost of delay for a slipping area's tasks

[strategy]
stale_after_days = 90         # 'last reviewed' older than this is a review finding
lag_flat_periods = 2          # consecutive unmet lag periods, with leads met, before "lead strong, lag flat"
```

## Event handling

- `email_classified` (existing, events CF): screen → triage (now with
  strategy; D14) → create with `serves:`/`role:` tags, or suppress on
  necessity in `suppress` mode → `task_changed` as today.
- `task_changed` (existing): gather → enrich (now with strategy) → claim and
  draft write-back of `serves:`/`role:` (D5) → rescore. The rescore loads the
  strategy once per process (cached with the standing-context text; a cold
  start picks up a new version).
- `day_changed` (existing): heal → settle deferrals → **evaluate goal state
  (D7) → create tripwire tasks (D8)** → rescore → run log.
- `POST /review` (new, webhook CF, escalate bearer): read `GET /review`'s
  data from the DB, render markdown, upsert the standing review task and
  post the comment (D11).

## Observability

Metrics (prefix `asana_`): `strategy_goals_loaded` (gauge, by kind),
`strategy_findings` (gauge), `necessity_judgments` (counter, by
confidence × outcome: attached | grooming | none), `tripwire_fired`
(counter, by goal), `goal_lead_value` (gauge, by goal × tag × window),
`area_below_the_line` (gauge, by area). Spans: `strategy.parse`,
`goal_state.evaluate`, `review.render`.

## Failure modes

| Failure | Behaviour |
|---|---|
| Strategy section missing/unreadable | no goals; necessity neutral; warning; ranking as today |
| A goal block malformed | that block skipped, rest loads; finding on the review |
| Model call fails | `serves` unenriched → neutral; no write-back; retried on next content change as today |
| Gate 2 fails or strategy unreadable there | gate 2 behaves exactly as today (fail-open); the task is judged by enrichment after creation |
| Asana write-back fails | claim already taken (same as points): logged, never retried — the grooming list shows the task as unattached |
| Tripwire task creation fails | raises → redelivery; external id prevents a duplicate on retry |
| DB down on `day_changed` | raises (prioritizer D7) |
| Lag never reported | `lag: null`, diagnosis `insufficient data`, no tripwire on `lag` can fire |

## Testing

- `tests/test_strategy.py`: parse a full example; a block missing `kind`;
  unknown kind; bad header line; duplicate id; empty section; every
  `lead`/`tripwire`/`below-the-line` grammar form, valid and invalid;
  `findings` for no-tripwire, no-lead, stale.
- `tests/test_goal_state.py`: lead counts over window edges; lag met/unmet;
  tripwire before/after `by`; each below-the-line signal; next-step and
  stalled; diagnosis rule including the two-period requirement.
- `tests/test_prioritize.py` (extend): each role × weight; `unattached`;
  `flag` renormalisation reproduces today's ordering on the existing
  fixtures; `stop_doing` bucket only in `suppress`; pin overrides it;
  goal-horizon due replaces priority horizon only when earlier and never when
  a hard or inferred date exists; below-the-line boost.
- `tests/test_enrichment.py` (extend): schema round-trip with `serves`;
  unknown goal id dropped; `content_hash` changes with strategy text;
  attach comment excluded from the hash.
- `tests/test_prioritize_handler.py` (extend, fakes): the four write-back
  rows of D5; the claim guard under redelivery; tripwire task created once
  across two days of `fired`; day-changed ordering.
- `tests/test_triage.py` (extend): schema round-trip with `serves`; unknown
  goal id dropped; strategy absent leaves the prompt and parse as today.
- `tests/test_task_create.py` (extend): the four rows of D14's table in each
  mode; the suppressed row's `source`; the `tasks` row carries the draft.
- `tests/test_api_review.py`: `GET /review` shape including
  `stop_doing.suppressed_emails`; `POST /goals/{id}/reports`.
- `scripts/test-review.py --dry-run`: renders the review against the live DB
  without posting.

## Rollout

1. Land the parser, config and schema with `mode = "flag"`; the scorer's
   output is unchanged by construction (renormalisation test).
2. Write `strategy.md` in the context repo (a separate piece of work, done
   with Ben: the consulting goal's diagnosis, guiding policy, path and
   tripwires; one block per area). Merge; cold start.
3. Run the heal; every task is judged once. Read the grooming and stop-doing
   lists; correct tags.
4. After a few weeks, read `calibrate`'s necessity agreement. Move to
   `demote`, then `suppress`, by config edit.

## Follow-ups (not in this spec)

- Lead tags that count *events* rather than tasks (a calendar conversation
  that never had a task). The schedule repo could publish `task-events`.
- A `task-review` agent that walks the grooming list interactively.
- Moving `[PX]` into a custom field, as the prioritizer spec already lists.

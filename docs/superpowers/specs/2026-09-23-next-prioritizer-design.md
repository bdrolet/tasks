# "What should I work on next" — event-driven WSJF prioritizer

**Date:** 2026-09-23
**Status:** designed, not implemented

## Problem

The service can answer "what is due" (the due-day digest, `POST /search` with
date bounds) and "what is late" (escalation). It cannot answer "what should I
do today". A task due in three weeks that needs a week of work is invisible
until it is nearly late; two tasks due the same day are indistinguishable
whatever their size; a task waiting on someone else sits in the list looking
like work. ~94 tasks are open across seven projects, almost all unassigned,
and the only ordering anyone has is due date.

## Goals

- A ranked "do next" list for today, sized to a daily capacity, plus three
  side lists: **overcommitted** (cannot make its deadline at current
  capacity), **stale / re-scope** (old low-priority work, repeatedly deferred
  work, soft deadlines that passed), and **nudge** (waiting on someone).
- Ranking is WSJF-style — cost of delay ÷ effort — where urgency comes from
  **slack** (time until due minus time needed), not raw days until due.
- Effort is a **story point** on the task, set by hand or estimated once by
  Claude as a visible draft. Points, not hours: a unit that is the same
  whether a person or the model wrote it.
- Everything is **event-driven**: a task change or a new day triggers
  gathering, enrichment and scoring for exactly what changed, so the read
  side is a database read that needs neither Asana nor Anthropic to be up.
- Every weight, horizon and threshold lives in one config file, because they
  will be tuned.
- The full ranked order is an API call, a Claude Code agent fronts it, and
  the order can be overridden by hand (pin a task to a position, snooze one)
  without fighting the scorer.
- A feedback loop: what was offered and not started, and cycle time per point
  at completion, so `calibrate` can say how wrong the estimates are.

## Non-goals

- **No hours tracking.** Nothing asks for or records `actual_hours`.
  Calibration measures cycle time per point (started → completed), which the
  fields already provide.
- **No changes to what becomes a task.** Gates 1 and 2, enrichment of email
  tasks, sections, recurrence and the digest are untouched.
- **No move of priority off the title.** `[PX]` stays the priority; the
  workspace's existing "Priority" enum custom field is not adopted here.
- **No planned start dates.** `start_on` is gathered and shown but never
  written (the 2026-09-09 start-dates spec remains its own work).
- **No per-person capacity or multi-user ranking.** One list, for Ben.

## Context

Asana is on the **Starter** plan (trial started 2026-09-23), which unlocks
custom fields, native task dependencies and start dates — all verified
against the API on 2026-09-23 (the workspace `custom_fields` listing returns
200; tasks return `dependencies`, `dependents`, `start_on`). Every project is
already in `ASANA_MANAGED_PROJECTS`, so each already has a webhook.

## Decisions

### D1 — Story points and started-at are Asana custom fields

Two workspace custom fields, attached to every managed project:

| Field | Type | Written by |
|---|---|---|
| `Story points` | number, precision 0 | Ben (UI, `task-next points`, `PATCH /tasks/{gid}`), or once by enrichment as a draft (D5) |
| `Started at` | date | Ben (UI, `task-next start`, `PATCH /tasks/{gid}`) |

Code resolves both by **name** (constants in `services/custom_fields.py`)
through a per-process cache of the workspace's custom-field listing; no GID
config. `scripts/setup_custom_fields.py` creates them if missing and adds
them to each managed project — run once per workspace, idempotent.

Why not tags (`sp:3`) or DB-only: Starter makes real fields available; they
are visible on the card, sortable, and a Rule can act on them. Why not
`start_on` for started-at: `start_on` is a *planned* start and can be set in
advance; started-at is a fact.

### D2 — One topic, one subscriber, one daily tick

Topic `task-events` (this repo's Terraform). Two message kinds:

```json
{"kind": "task_changed", "gid": "1218…", "source": "webhook|api|pipeline|heal"}
{"kind": "day_changed"}
```

`day_changed` carries no date: the subscriber takes `today` from
`services/due_digest.today_local()`, so the scheduler body is static.

Publishers of `task_changed`: the Asana webhook (any task or story event on a
managed project), `handlers/task_create.py` after creation, and the
`api/routers/tasks.py` / `comments.py` write paths after any mutation. All
three already call `services/task_index.refresh(gid)` at exactly that point;
the publish sits beside it. Publishing is best-effort at the publisher (log
and continue — the daily heal catches anything dropped, D8).

`day_changed` is published by Cloud Scheduler `tasks-day-changed`
(`45 5 * * *` America/New_York, Pub/Sub target — no HTTP, no bearer).

Subscriber: a third Cloud Function **`tasks-prioritize`**, Pub/Sub trigger
on `task-events`, entry point `prioritize` in `main.py`, body in
`handlers/prioritize.py`. It has the DB, `asana-api-key` and
`tasks-anthropic-api-key` mounted.

Why Pub/Sub rather than working inside the webhook: Asana wants a reply in
10 s; gathering plus an occasional model call is 3–8 s before retries. The
webhook already only flips a flag for the digest for the same reason. A
topic also gives the three write paths one consumer.

### D3 — Facts, enrichment and scores are separate tables

| Table | Rewritten when | Holds |
|---|---|---|
| `task_facts` | every gather | deterministic Asana facts |
| `task_enrichment` | content hash moves | raw model JSON |
| `task_overrides` | `PUT /tasks/{gid}/overrides` | manual field overrides, pin, snooze (D11) |
| `task_scores` | every rescore | the whole scored set for today |
| `prioritize_runs` | every rescore | run log (top-N; full set on the daily run) |
| `task_stats` | completion; daily tick | deferral counter, cycle-time snapshot |

`task_index` and `tasks` are unchanged. Schema in §Data model.

### D4 — Scoring is materialised, and it is a pure function

`services/prioritize.py` is I/O-free: `score_set(facts, enrichments,
overrides, stats, config, today) → ScoredSet`. The subscriber runs it over
the full set after every message and rewrites `task_scores`; `POST /next`
reads that table and applies only the read-time knobs (`--energy`, `--n`).
At ~100 tasks a rescore is milliseconds; concurrent rescores are last-writer-
wins over near-identical inputs, which is harmless.

### D5 — Enrichment: one schema-constrained call per task per content change

Model: **`claude-opus-5`**, adaptive thinking, `output_config.effort = "low"`,
`output_config.format` = JSON schema (§Enrichment). Chosen for judgment on
point estimates and impact — every estimate lands in Asana where Ben sees
it — at a volume (~100 calls once, a few per day, ≈$1 then pennies) where
the price difference to a smaller model is not a consideration. A new
`clients/claude.py::extract_structured(model=…)` carries it; `classify()`
stays Haiku-pinned with `temperature=0`, which Opus 5 rejects.

Cache key: `sha256(name + "\n" + notes + "\n" + comments)`, comments in
created order, **excluding** the one comment this service posts (the
estimate note, D6) so the write-back cannot re-trigger enrichment. Same
hash → no call. The raw JSON is stored verbatim so fields can be added
without re-extracting.

Effective value for every enrichable field: **tag** (`waiting:<who>`,
`energy:deep|shallow`, `impact:low|medium|high`) > **`task_overrides`**
(set through the API) > **model** > **default**.

### D6 — The model may write story points, once, as a draft

If `Story points` is empty **and** `task_facts.points_estimated IS NULL`,
the subscriber writes the suggestion to the field and posts the comment
`Estimated {n} points — adjust if wrong.` The guard is a conditional
`UPDATE … WHERE points_estimated IS NULL` whose rowcount decides who writes,
so a redelivered or concurrent message cannot write twice. A later field
value that differs from `points_estimated` means Ben set it; both values are
kept so `calibrate` can compare them. Never written again for that task.

### D7 — The subscriber requires the database

Unlike most handlers, `handlers/prioritize.py` lets a DB failure **raise**:
its whole job is writing these tables, and a raise makes Pub/Sub redeliver.
Asana failures raise for the same reason. An Anthropic failure does **not**
raise: facts and scores still update, the enrichment row is left as it was,
the task is flagged `unenriched`, and the daily heal republishes it (D8).

### D8 — The daily tick heals and closes the loop

On `day_changed`, before rescoring:

1. **Deferrals.** For each task in the previous canonical run's top-N: if
   `Started at` is set (any date — an open task with a start date is in
   progress, not deferred) or `completed_at` ≥ that day, mark it `started`
   in the run row; otherwise `task_stats.times_deferred += 1`. Interactive `/next`
   calls log a `manual` run but never bump the counter — only the daily run
   is an "offer".
2. **Heal.** List open tasks per managed project (plus one level of subtasks
   for parents with `num_subtasks > 0`) and publish `task_changed` for any
   task whose `modified_at` is newer than `task_facts.fetched_at`, that has
   no facts row, or whose enrichment hash ≠ its current content hash.
3. Rescore for the new date and write the canonical run (kind `daily`, full
   set).

### D9 — Candidate set and "actionable"

Candidates are the open tasks of every project in `ASANA_MANAGED_PROJECTS`,
plus their subtasks one level down (in the parent's project). A task is
excluded from "do next", with a reason, when:

| Reason | Rule |
|---|---|
| `blocked` | any native dependency is open |
| `parent` | it has open subtasks (its subtasks are the work) |
| `waiting` | `waiting_on` is set → **nudge** list instead |
| `completed` | completed (kept in facts for calibration) |

There is no other status: Asana has none.

### D10 — Priority comes from the title; no prefix means P2

`[PX]` is parsed as `services/due_digest._priority_rank` does. A task with
no prefix scores as `config.default_priority` (P2). The horizon for a soft
deadline uses the same value.

### D11 — The order can be overridden by hand: pins and snoozes

Two manual controls, stored per task in `task_overrides` alongside the
enrichment overrides (D5), applied by the pure scorer after scoring and
before selection:

| Override | Effect | Cleared |
|---|---|---|
| `pinned_rank: N` | the task holds position N in `next` regardless of score or capacity; the greedy pass fills the remaining positions around pins | by hand, or automatically when the task completes |
| `snooze_until: YYYY-MM-DD` | bucket `snoozed` — out of `next` and the side lists until that date | when the date passes |

Two pins on the same position keep their relative order by score. A pinned
task that is blocked or waiting still shows in `next`, flagged, because a
pin is an explicit instruction. Pins and snoozes are visible in `--explain`
and in `GET /ranking` as `override`. Nothing else edits the order: there is
no drag-to-reorder state to keep in sync, only these two fields.

### D12 — An agent fronts the API

`task-next` is a Claude Code agent in `.claude/agents/`, dispatched for
"what should I work on", "what's next", "why is X ranked there", "pin X to
the top", "snooze X until Friday", "point X at 3", "I started X". It reads
`GET /ranking` / `POST /next` and performs only the small writes that
express an ordering decision (pin, unpin, snooze, points, started-at,
overrides) through the API. It never creates, edits text, completes or
comments — those stay with the existing agents. Backed by a
`prioritizing-tasks` skill for direct use, both symlinked by
`scripts/link-skills.sh`, and listed in CLAUDE.md's standing dispatch.

## Components

| Path | Layer | Role |
|---|---|---|
| `config/prioritize.toml` | config | every weight, horizon, threshold (§Config); loaded by `services/prioritize_config.py` with stdlib `tomllib`; path from `PRIORITIZE_CONFIG_PATH` (defaults to the repo file) |
| `models/prioritize.py` | models | `TaskFacts`, `Enrichment`, `ScoredTask`, `ScoredSet` dataclasses |
| `services/custom_fields.py` | services | field names → gids (cached), read/write value shapes for number and date fields |
| `services/enrichment.py` | services | content hash, prompt, schema, `extract()` → validated `Enrichment`, effective-value merge (tags > overrides > model > default) |
| `services/prioritize.py` | services | **pure**: filter, feasibility, score, select, side lists |
| `services/prioritize_config.py` | services | TOML → frozen `Config` dataclass |
| `clients/claude.py` | clients | `extract_structured(*, model, system, user, schema, effort)` |
| `clients/asana.py` | clients | `PRIORITIZE_OPT_FIELDS`; `set_custom_field(gid, field_gid, value)`; `list_custom_fields()`; `add_custom_field_to_project()`; `create_custom_field()`; widened `WEBHOOK_FILTERS` |
| `clients/pubsub.py` | clients | thin publisher (port of inbox's; trace context in attributes) |
| `repo/prioritize.py` | repo | the five tables |
| `handlers/prioritize.py` | handlers | `handle(message)`: dispatch on `kind`; gather → enrich → write-back → rescore; daily tick |
| `handlers/asana_webhook.py` | handlers | publish `task_changed` per distinct gid; story events map to their parent task |
| `handlers/task_create.py`, `api/routers/tasks.py`, `api/routers/comments.py` | handlers / routers | publish after each write |
| `api/routers/next.py` | routers | `GET /ranking`, `POST /next`, `GET /calibrate`, `PUT /tasks/{gid}/overrides` |
| `.claude/agents/task-next.md`, `.claude/skills/prioritizing-tasks/` | consumer | the agent and skill (D12); `scripts/link-skills.sh` symlinks both |
| `api/routers/tasks.py` | routers | `story_points`, `started_at` on create / update / detail; `dependencies`, `start_on` on detail |
| `scripts/task_next.py` → `task-next` | scripts | stdlib CLI over the API |
| `scripts/setup_custom_fields.py` | scripts | one-time field creation |
| `scripts/backfill_prioritize.py` | scripts | publish `task_changed` for every candidate (initial load) |
| `main.py` | entry points | `prioritize` cloud-event entry point; `otel.flush()` in `finally` |
| `terraform/pubsub.tf`, `cloud_functions.tf`, `scheduler.tf`, `iam.tf` | infra | topic, CF, scheduler, publisher/subscriber IAM |

## Data model

```sql
-- Deterministic Asana facts for every task in a managed project (and their
-- subtasks). Rewritten on every gather. priority is the [PX] prefix or NULL.
CREATE TABLE IF NOT EXISTS task_facts (
    task_gid           TEXT PRIMARY KEY,
    project_gid        TEXT,
    project_name       TEXT,
    parent_gid         TEXT,
    name               TEXT NOT NULL,
    priority           TEXT,                 -- 'P0'..'P3' or NULL
    due_on             DATE,
    due_at             TIMESTAMPTZ,
    start_on           DATE,
    started_at         DATE,                 -- "Started at" custom field
    story_points       INTEGER,              -- "Story points" custom field
    points_estimated   INTEGER,              -- what enrichment wrote, once; NULL = never
    completed          BOOLEAN NOT NULL DEFAULT false,
    completed_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL,
    modified_at        TIMESTAMPTZ NOT NULL,
    tags               JSONB NOT NULL DEFAULT '[]',   -- names
    dependencies       JSONB NOT NULL DEFAULT '[]',   -- gids
    dependents         JSONB NOT NULL DEFAULT '[]',   -- gids
    num_open_subtasks  INTEGER NOT NULL DEFAULT 0,
    content_hash       TEXT NOT NULL,        -- D5 hash of the text this gather saw
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Model output, cached by content hash. Never carries manual state.
CREATE TABLE IF NOT EXISTS task_enrichment (
    task_gid     TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    raw          JSONB NOT NULL,             -- verbatim model JSON
    model        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Manual state, written only by PUT /tasks/{gid}/overrides (D5 field
-- overrides; D11 pinned_rank / snooze_until). Survives re-enrichment and
-- re-gathering; pinned_rank is cleared by the subscriber on completion.
CREATE TABLE IF NOT EXISTS task_overrides (
    task_gid     TEXT PRIMARY KEY,
    overrides    JSONB NOT NULL DEFAULT '{}',   -- {waiting_on, impact, energy, story_points, due_date_inferred, ...}
    pinned_rank  INTEGER,
    snooze_until DATE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The whole scored set, rewritten on every rescore. GET /ranking and
-- POST /next read only this.
CREATE TABLE IF NOT EXISTS task_scores (
    task_gid     TEXT PRIMARY KEY,
    scored_at    TIMESTAMPTZ NOT NULL,
    today        DATE NOT NULL,
    bucket       TEXT NOT NULL,   -- 'next' | 'nudge' | 'snoozed' | 'excluded:blocked' | 'excluded:parent' | 'excluded:completed'
    score        DOUBLE PRECISION,
    position     INTEGER NOT NULL, -- 1-based order in the full ranking (pins first at their rank, then by score)
    rank         INTEGER,         -- position within the default `next` selection, NULL if not selected
    components   JSONB NOT NULL,  -- P,U,I,B,A,C, points, points_source, effort_days, effective_due, soft,
                                  -- days_until_due, slack, simulated_start, effective_slack, days_stale,
                                  -- energy, unenriched, override {pinned_rank?, snooze_until?, fields: [...]}
    overcommitted BOOLEAN NOT NULL DEFAULT false,
    stale         BOOLEAN NOT NULL DEFAULT false,
    stale_reason  TEXT
);

-- Run log. Event runs keep the top-N; the daily run keeps the full set and
-- is the one deferrals are counted against (D8).
CREATE TABLE IF NOT EXISTS prioritize_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,               -- 'event' | 'daily' | 'manual'
    ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    today       DATE NOT NULL,
    trigger_gid TEXT,
    top         JSONB NOT NULL               -- [{gid, rank, score, components, started}]
);

-- Feedback: deferrals and the completion snapshot calibrate reads.
CREATE TABLE IF NOT EXISTS task_stats (
    task_gid             TEXT PRIMARY KEY,
    project_name         TEXT,
    times_deferred       INTEGER NOT NULL DEFAULT 0,
    last_offered         DATE,
    started_at           DATE,
    completed_at         TIMESTAMPTZ,
    points_at_completion INTEGER,
    points_estimated     INTEGER,
    cycle_days           DOUBLE PRECISION     -- completed_at - started_at, NULL if never started
);
```

Deleted / removed tasks (webhook `deleted`/`removed`) drop their
`task_facts`, `task_enrichment` and `task_scores` rows; `task_stats` and
`prioritize_runs` are history and are kept.

## Event handling (`handlers/prioritize.py`)

### `task_changed`

1. **Gather.** `asana.get_task_detail(gid)` with `PRIORITIZE_OPT_FIELDS`
   (`DETAIL_OPT_FIELDS` + `custom_fields`, `dependencies.gid`,
   `dependencies.completed`, `dependents.gid`, `start_on`, `completed_at`)
   and `asana.get_stories(gid)`. A 404 deletes the rows and returns. For a
   parent with `num_subtasks > 0`, `get_subtasks` and gather each subtask
   the same way (they carry the parent's project). Upsert `task_facts`. If
   the task just became completed, snapshot `task_stats` (D8's cycle time)
   and clear its `pinned_rank` (D11).
2. **Enrich** (skipped for completed tasks). Compute the content hash; if it
   equals `task_enrichment.content_hash`, skip. Otherwise call
   `services/enrichment.extract`, validate, upsert the row with the new hash.
   Any failure: log, `otel.errors`, leave the row, continue.
3. **Write back** (D6) if the field is empty, the model returned a
   suggestion and the conditional update wins. This triggers another
   webhook → `task_changed`, which gathers, finds the hash unchanged and
   only rescores.
4. **Rescore** (D4) and append an `event` run row (top-N).

### `day_changed`

D8 steps 1–3. The heal step may publish many messages; each is an ordinary
`task_changed`.

### Webhook changes

`clients/asana.py::WEBHOOK_FILTERS` widens the `changed` fields to
`completed, name, notes, due_on, due_at, start_on, custom_fields,
dependencies, tags` and adds `{"resource_type": "story", "action": "added"}`.
`handlers/asana_webhook.py::receive` publishes one `task_changed` per
distinct task gid in the delivery; a story event contributes its
`parent.gid`. Existing behaviour (completion handling, digest dirty flag,
`task_index` refresh) is unchanged.

`services/webhook_registry.py` / `handlers/webhook_sync.py` learn to compare
a registration's `filters` to `WEBHOOK_FILTERS` and re-register on a
mismatch, so the wider filters roll out through the existing daily
reconciler (plus a manual `POST /webhook-sync` after deploy).

## Enrichment (`services/enrichment.py`)

**Prompt.** System: what the fields mean and the point scale (1 = under an
hour of focused work; 2 = a morning; 3 = a day; 5 = several days; 8 = a
week or more, should probably be split), that comments from the service
account are automated, that `waiting_on` means an external party must act
before Ben can, that an inferred due date needs an explicit statement in the
text and never a guess, and today's date. User: title, project, notes
(plain text via `services/task_bullets.description_text`, capped 6k chars),
each comment as `[date] author: text` (capped 3k chars total, newest kept),
due/start dates, tags.

**Schema** (`output_config.format`, strict):

```json
{
  "story_points_suggested": 1 | 2 | 3 | 5 | 8,
  "points_confidence": "low" | "medium" | "high",
  "waiting_on": string | null,
  "due_date_inferred": "YYYY-MM-DD" | null,
  "due_date_inferred_confidence": "low" | "medium" | "high",
  "impact": "low" | "medium" | "high",
  "energy": "deep" | "shallow",
  "latest_comment_signal": "none" | "unblocked" | "new_deadline" | "scope_change",
  "reason": string
}
```

`reason` is one sentence for `--explain`; it is not scored. Validation is a
pydantic model on our side as well as the API's schema constraint; a
response that fails either is treated as a failed call.

**Defaults when unenriched:** points `config.default_points` (3) with
`low` confidence, no waiting-on, no inferred due, impact medium, energy
shallow, signal none; `components.unenriched = true`.

## Scoring (`services/prioritize.py`, pure)

All in days; `today` is `services/due_digest.today_local()` (America/Los_Angeles).

**Effort.** `points` = field value if set, else `points_estimated`, else
default. `effort_days = points / capacity_points_per_day`; if the points
came from an estimate (or default) with `low` confidence, × `low_confidence_multiplier` (1.5).

**Effective due.** `due_on` (hard) → else `due_date_inferred` with
medium/high confidence (soft) → else `created_at + horizon[priority]` (soft)
→ else none.

**Feasibility.** Candidates sorted by effective due (none last), simulated
back-to-back from today: `simulated_start` = cumulative effort before it;
`effective_slack = days_until_due − (simulated_start + effort_days)`.
Negative → `overcommitted` (still scored, still eligible).

**Components** (0–1):

```
P = priority_weight[priority]            # P0 1.0, P1 0.6, P2 0.3, P3 0.1
U = 1 / (1 + exp(k * (effective_slack − s0)))   # k 1.0, s0 3
    soft due → min(U, soft_urgency_cap)  # 0.6
    no due   → no_due_urgency            # 0.1
A = min(1, days_stale / stale_days)      # days_stale = today − modified_at; 30
C = category_weight[project_name] or default_category_weight   # 0.5
I = impact_weight[impact]                # 0.2 / 0.5 / 1.0
B = min(1, unblock_per_task * open dependents)  # 0.3 each
cost_of_delay = wP*P + wU*U + wI*I + wB*B + wA*A + wC*C   # .30 .30 .15 .10 .10 .05
score = cost_of_delay / max(effort_days, min_effort_days)   # 0.25
```

**Manual order (D11).** A task with `snooze_until > today` gets bucket
`snoozed` and takes no further part. Pinned tasks are placed first, at
their `pinned_rank` (ties by score), and count toward neither capacity nor
`n`.

**Ranking.** Every remaining candidate ordered by score descending; pins
occupy their positions ahead of it. This order is `position` in
`task_scores` and what `GET /ranking` returns.

**Selection.** Greedy by score through the unpinned candidates until
`sum(points) ≥ capacity_points_per_day` or `n` reached; after each pick,
remaining tasks in the same project × `diversity_penalty` (0.8). With
`--energy`, mismatched tasks × `energy_penalty` (0.7) before the greedy
pass. The stored `rank` is the selection with no energy flag and the config
`default_n`; `POST /next` reruns the greedy pass from stored components
when either knob is given. Pins are never displaced by the knobs.

**Side lists.**

- overcommitted: `effective_slack < 0`
- stale: `priority ∈ {P2, P3} and days_stale > stale_after_days (45)`, or
  `times_deferred ≥ deferred_limit (5)`, or a **soft** effective due already
  past; `stale_reason` records which
- nudge: bucket `nudge`, sorted by `days_stale` descending
- snoozed tasks appear in none of them

## Read side

**`GET /ranking`** `?limit=100&offset=0&bucket=next|nudge|snoozed|excluded&explain=false`
→ every scored task in `position` order:

```json
{"today": "…", "scored_at": "…", "total": 94,
 "tasks": [{position, rank, task_gid, name, project, permalink_url, bucket,
            score, points, points_source, due_on, effective_due, soft,
            overcommitted, stale, stale_reason, waiting_on,
            override: {pinned_rank?, snooze_until?, fields?},
            components?, reason?}]}
```

The default `bucket` filter is `next` (the actionable ranking); `excluded`
covers every `excluded:*` bucket. A `list=overcommitted|stale|nudge`
parameter returns one side list on its own, in its natural order
(overcommitted and stale by `position`; nudge by `days_stale` descending),
since overcommitted and stale are flags on tasks that may also be in `next`
rather than buckets of their own. `bucket` and `list` are mutually
exclusive (400 if both). This is the "tasks in ranked order" call;
`POST /next` below is the daily view over it and always carries all three
side lists.

**`POST /next`** `{energy?: "deep"|"shallow", n?: int, explain?: bool}` →

```json
{"today": "…", "scored_at": "…", "run_id": 123,
 "next": [{task_gid, name, project, permalink_url, points, points_source,
           due_on, effective_due, soft, score, rank, components?, reason?}],
 "overcommitted": [...], "stale": [{…, "stale_reason"}], "nudge": [{…, "waiting_on", "days_stale"}],
 "unenriched": <count>}
```

Logs a `manual` run. `components` and `reason` only with `explain`.

**`GET /calibrate`** → per project: completed-with-points count,
mean/median `cycle_days / points`, mean `points_at_completion /
points_estimated` where both exist, `times_deferred` distribution, and the
overall figures. Plain numbers; the multiplier is applied by editing config.

**`PUT /tasks/{gid}/overrides`** `{field: value | null, …}` — merges into
`task_overrides`; null clears. Fields: the enrichable ones (`waiting_on`,
`impact`, `energy`, `due_date_inferred`, `story_points`) plus `pinned_rank`
and `snooze_until` (D11). Publishes `task_changed` so the new order is
materialised within seconds. Rejects unknown fields (422), as
`UpdateTaskRequest` does.

**`PATCH /tasks/{gid}`** gains `story_points: int | null` and
`started_at: "YYYY-MM-DD" | null`; **`POST /tasks`** gains `story_points`;
**`GET /tasks/{gid}`** gains `story_points`, `started_at`, `start_on`,
`dependencies` (gid + name + completed).

**`task-next`** (`scripts/task_next.py`, stdlib, on PATH via
`scripts/link-skills.sh`):

```
task-next [--energy deep|shallow] [--n N] [--explain]   # the lists, ref-first
task-next ranking [--all] [--explain]                   # full order (GET /ranking)
task-next start <ref|gid>                               # Started at = today
task-next points <ref|gid> <n>
task-next pin <ref|gid> <position> | unpin <ref|gid>
task-next snooze <ref|gid> <YYYY-MM-DD> | unsnooze <ref|gid>
task-next override <ref|gid> field=value [field=…]      # field= clears
task-next calibrate
```

Refs are `scripts/task_ref.py` refs over the scored set, computed by the
CLI and the agent (the API never returns a ref — `task_ref.py` stays the one
implementation); every write subcommand resolves a ref against the current
`/ranking` response and sends the GID. Output is the same ref-first TSV shape `task-ref` produces, one
block per list.

**`task-next` agent** (`.claude/agents/task-next.md`, D12) and the
`prioritizing-tasks` skill (`.claude/skills/prioritizing-tasks/SKILL.md`):
the agent translates "what should I work on / what's next / why is X there /
pin X / snooze X till Friday / X is 3 points / I started X" into the calls
above, resolves refs and names against `/ranking`, and returns the ref-first
listing. It is read-mostly: its only writes are `PUT /tasks/{gid}/overrides`
and the `story_points` / `started_at` fields on `PATCH /tasks/{gid}`. It
does not create, rename, complete or comment — it hands those to the
existing agents. CLAUDE.md's standing dispatch gains: a request to **rank
or choose** work ("what should I do next", "what's my day look like",
"bump X up") goes to the `task-next` agent.

## Config (`config/prioritize.toml`)

```toml
[capacity]
points_per_day = 5
default_points = 3
low_confidence_multiplier = 1.5
min_effort_days = 0.25

[weights]           # cost_of_delay terms
priority = 0.30
urgency = 0.30
impact = 0.15
unblock = 0.10
aging = 0.10
category = 0.05

[priority]          # P
P0 = 1.0
P1 = 0.6
P2 = 0.3
P3 = 0.1
default = "P2"

[horizon_days]      # soft deadline from created_at
P0 = 3
P1 = 14
P2 = 45
P3 = 120

[urgency]
k = 1.0
s0 = 3.0
soft_cap = 0.6
no_due = 0.1

[impact]
low = 0.2
medium = 0.5
high = 1.0

[aging]
stale_days = 30

[unblock]
per_task = 0.3

[category]          # keyed by Asana project name; unknown → default
default = 0.5

[selection]
default_n = 5
diversity_penalty = 0.8
energy_penalty = 0.7

[stale]
after_days = 45
deferred_limit = 5
```

Project names are not personal (they appear in existing specs); a project
that wants a weight other than 0.5 is added under `[category]`.

## Infrastructure

- `terraform/pubsub.tf`: `google_pubsub_topic.task_events`
  (`task-events`), with `roles/pubsub.publisher` for the `tasks-events-cf`,
  `tasks-webhook-cf` and `tasks-api` service accounts.
- `terraform/cloud_functions.tf`: `google_cloudfunctions2_function.tasks_prioritize`
  — same source zip, entry point `prioritize`, `event_trigger` on the topic
  with `RETRY_POLICY_RETRY`, 120 s timeout, 512Mi, max 3 instances, its own
  service account `tasks-prioritize-cf` with `cloudsql.client` and
  `secretAccessor` on `asana-api-key`, `tasks-anthropic-api-key`,
  `tasks-db-password`, `grafana-otlp-*`. `common_env` gains nothing new;
  `PRIORITIZE_CONFIG_PATH` is unset (repo default).
- `terraform/scheduler.tf`: `google_cloud_scheduler_job.day_changed`,
  `45 5 * * *` America/New_York, `pubsub_target` with the JSON body above
  (date is filled in by the subscriber from `today_local()`, so the body is
  static).
- `deploy.yml` needs no change (Terraform-driven); `deploy-api.yml` is
  unchanged. `requirements.txt` gains `google-cloud-pubsub`.
- One-time after apply: `scripts/migrate_db.py`, `scripts/setup_custom_fields.py`,
  `POST /webhook-sync` (re-registers with the wider filters),
  `scripts/backfill_prioritize.py` (publishes ~100 `task_changed`).

## Observability

Span `tasks.prioritize` per message, continuing the publisher's trace from
the message attributes. Metrics: `asana_prioritize_events_total{kind,result}`,
`asana_prioritize_enrich_total{result=cached|ok|failed|written_back}`,
`asana_prioritize_rescore_duration_milliseconds`,
`asana_prioritize_candidates{bucket}` (last-value). Model tokens go through
the existing `claude_tokens` counter.

## Failure modes

| Failure | Behaviour |
|---|---|
| Asana down during gather | raise → Pub/Sub redelivers (bounded by the retry policy) |
| Anthropic down / bad JSON | facts + scores still written; enrichment row untouched; `unenriched`; healed daily |
| DB down | raise → redeliver (D7) |
| Publish fails at a producer | logged; heal republishes within a day |
| Webhook dropped by Asana | existing `webhook-sync` re-registers; heal covers the gap |
| Custom field missing from a project | gather reads null; write-back skipped with a warning naming `setup_custom_fields.py` |
| Duplicate / concurrent messages | gather and rescore are idempotent; write-back guarded by the conditional update (D6) |

## Testing

`tests/test_prioritize.py` (pure, dict fixtures like `test_due_digest.py`):

- same due date, different points → the shorter-slack task ranks higher
- negative slack saturates `U` and sets `overcommitted`
- a soft deadline never yields `U > 0.6`; no deadline yields `0.1`
- blocked and parent tasks are excluded with the right reason; a
  waiting-on task lands in `nudge`
- diversity penalty: a set of eight tasks in one project and two in another
  yields a mixed-project top 5
- energy flag demotes mismatched tasks; `n` caps the list
- stale rules: each of the three triggers, and their `stale_reason`
- tag > override > model > default precedence
- a pinned task holds its position over a higher-scoring one and beyond
  capacity; two pins on one position order by score; a pinned blocked task
  still appears, flagged
- a snoozed task is in no list until its date; on the date it returns

`tests/test_enrichment.py`: hash is stable across an added estimate comment
and changes on any other comment; a cached hash makes no model call
(fake client); a schema-violating response is a failed call, not a cached
one.

`tests/test_prioritize_handler.py` (fakes for Asana, DB, publisher, model):
`task_changed` gathers, enriches once, writes back once across two
deliveries, and rescores; `day_changed` bumps `times_deferred` for an
un-started offer and not for a started one, and republishes stale facts.

`tests/test_asana_webhook.py`: a story `added` event publishes the parent's
gid; a delivery with three events for one task publishes once.

`tests/test_api_next.py`: `/ranking` order, bucket filter and paging;
`/next` shape, `explain`, `energy`/`n` re-selection from stored components
with pins untouched; `/calibrate` arithmetic; overrides merge, clear, and
422 on an unknown field; completion clears `pinned_rank`.

## Dependencies

One new runtime dependency: **`google-cloud-pubsub`** (the publisher; the
subscriber side is functions-framework, already present). `tomllib` and
`pydantic` are already available.

## Follow-ups (not in this spec)

- Move priority into the "Priority" enum custom field and drop the `[PX]`
  prefix.
- An Asana Rule "Started at set → move to In progress" if a visual column is
  wanted.
- Use calibrate's per-project multiplier automatically rather than by
  editing config.
- Revisit `days_stale`: `modified_at` is bumped by this service's own writes
  (points, section moves, escalation), so aging is optimistic.

# tasks

Asana task automation service. Inbox publishes `email_classified` domain
events for **every** processed email to its `email-events` Pub/Sub topic;
this repo owns the task policy ("should this be a task" — `services/screening.py`),
Claude enrichment (summary + deadline), and all Asana interactions. Dividing
rule: mailbox-touching work (classification, invite detection, reply drafts)
stays in inbox; task-serving work lives here.

## Stack

| | |
|---|---|
| **GCP project** | `bens-project-462804`, `us-central1` |
| **Events CF** | `tasks-events` — Pub/Sub trigger on the inbox-owned `email-events` topic, entry point `process` in `main.py` |
| **Enrichment** | Claude via `clients/claude.py` (Haiku summary, Sonnet deadline extraction) — `ANTHROPIC_API_KEY` |
| **Embeddings** | Vertex AI `gemini-embedding-001` via `clients/vertex.py` (IAM auth — no key/secret; `VERTEX_*` env vars with in-code defaults). Corpus in `task_index`, maintained by `services/task_index.py::refresh` from pipeline/API/webhook write paths; seed/heal with `scripts/backfill_embeddings.py`. `POST /search` `semantic: true` ranks by cosine nearest-neighbor, falls back to substring on Vertex outage |
| **Webhook CF** | `tasks-webhook` — HTTP public, entry point `webhook` in `main.py` (same source zip) |
| **API** | `tasks-api` — Cloud Run FastAPI service (`api/`), search/fetch/add/update for tasks + comments; list/create projects, list tags, subtasks; bearer auth via `tasks-api-token`; image in AR repo `tasks`, deployed by `deploy-api.yml`; `tasks-api.drolet.cloud` |
| **Escalation** | Cloud Scheduler `tasks-escalation`, `0 6 * * *` America/New_York → `POST <webhook-url>/escalate` |
| **Digest** | Cloud Scheduler `tasks-digest`, `*/10 * * * *` → `POST <webhook-url>/digest` (same bearer as escalate) — rebuilds the due-day calendar digest when the Asana webhook has set `digest_state.dirty_at` or the last rebuild is > 60 min old; writes through `clients/schedule_api.py` (`SCHEDULE_API_URL`/`SCHEDULE_API_TOKEN`, secret owned by schedule terraform) |
| **Database** | `tasks` DB + `tasks` user on Cloud SQL `bens-project-462804:us-central1:inbox` (Postgres 16, instance owned by inbox terraform) — tables `tasks`, `asana_tag_cache`, `task_index` (pgvector semantic-search corpus), `due_day_events`, `task_bullets`, `digest_state`; schema in `repo/schema.sql` |
| **Observability** | OTel → Grafana Cloud OTLP; metrics prefixed `asana_` |
| **Infra** | `terraform/` — GCS backend `bens-project-462804-tf-state`, prefix `tasks` |

## Event schema (see models/events.py)

- `email_classified` — one per processed email, ALL categories
  (urgent|respond|review|reference|ignore): message_id, category, importance
  (P0–P3), confidence, subject/sender/sender_display/to/cc/received_at, tags, reasoning, full
  body (10k cap) + body_html (200k cap), web_link, draft_link? (respond),
  seed_key_points?/seed_links? (calendar-invite facts + RSVP links from inbox)
- `label_applied` — message_id, task_gid (nullable — resolved via the tasks DB,
  then Asana `external:{message_id}` lookup), label, source
- Completions arrive via the Asana webhook, not Pub/Sub.

## Task policy

`services/screening.py::screen` — gate 1. A Haiku call over **every** email
inbox publishes, whatever category it was filed under. It reads the email, its
attachment metadata (names/types/sizes, fetched via `graph_message_id` — NOT
`message_id`, which is inbox's UUID and gets rejected), and the `Roles` section
of the declared facts, and returns a three-way verdict:

- `task` → gate 2 (`services/triage.py`), then enrichment and creation.
- `relate` → `services/relating.py`: embed the email, take the nearest open
  tasks from `task_index`, apply a similarity floor (`SIMILARITY_FLOOR = 0.65`,
  tuned against a measured 1,266-email run), confirm with one Haiku call, verify
  the gid against Asana. A match becomes a **comment** on that task via
  `_suppress()`'s related-task branch; no match is a normal outcome and still
  records a row. Nothing here ever closes a task.
- `drop` → a `suppressed_emails` row.

Tasks owns its own priority from here: the `[PX]` prefix and the P0/P1 deadline
gate read the screener's verdict, not inbox's `importance`. Section placement
still reads inbox's `category` (a mailbox-routing fact); a rescued email (one
`screen` kept that inbox filed as ignore/reference) lands in Review, but only
where the caller opts in — `services/sections.py::for_category(category, *,
default=False)` defaults to no section, and only `handlers/task_create.py`
passes `default=True`. `handlers/label_applied.py` deliberately does not: it
passes a *label*, where `ignore`/`reference` mean "no section move", not "move
it to Review". `services/policy.py::warrants_task` is retained as the OUTAGE
fallback — a Claude failure degrades gate 1 to the old category rule
(`task`/`drop` only, never `relate`) rather than flooding the list.

**There is no urgent bypass.** `urgent` mail runs gate 2 like everything else.

Gate 1 also returns an `audience` (`self`|`shared`), judged against the
`Calendar Routing` section of the declared facts. `services/shared_tags.py`
adds the `cheryl` tag when the audience is `shared` **or** Cheryl is on the
email (`CHERYL_EMAILS`, a comma-separated list from tfvars → CF env, never
committed) — that tag is what routes the task's due-day digest to the shared
calendar. Manual tasks get it from the `task-builder` agent's tagging rule.

Then `services/triage.py::decide` (gate 2) — a Sonnet 5 tool-runner agent with
read-only `search_emails` / `get_email` / `search_tasks` / `get_task` tools
that reads the `Roles` section of the declared facts and decides whether the
email still requires anything; non-actionable emails are recorded
in `suppressed_emails` (optionally attached to a related task as a comment,
after `decide` verifies the model-supplied `related_task_gid` against Asana —
an unfetchable gid is treated as no match) and never created. Fail-open
everywhere. A deterministic no-action-phrase veto (`policy.no_action_phrase`)
runs on the Haiku key points as a backstop; its autopay/automatic-payment
patterns are conditional — they only veto a key point that carries no failure
or attention-needed language (see `CONDITIONAL_NO_ACTION_PATTERNS` in
`services/policy.py`). Changing what becomes a task is a change HERE, never an
inbox deploy. Changing a **declared fact** is a PR in the private
`bdrolet/context` repo: its CI publishes the `standing-context` secret, which
`terraform/cloud_functions.tf` mounts read-only at
`/etc/context/standing-context.md` on the events CF (`STANDING_CONTEXT_PATH`
points there; `services/standing_context.py` just reads a path). Facts are
personal and this repo is public — never commit one here; `context/` holds only
a README and an example, and is otherwise gitignored. A fact edit needs no
deploy of this service, only a cold start. Enrichment (summary via Claude
Haiku, deadline extraction for P0/P1 via Sonnet — the latter reads the
`Calendar` section) runs only for events that pass both gates. Design:
`docs/superpowers/specs/2026-08-18-standing-context-gate-design.md`,
`docs/superpowers/specs/2026-08-27-tasks-owned-screening-design.md`.

## Task title standard

Pipeline task titles are `[PX] {verb} {object}` — a context-driven action, not
the raw subject. Authoritative definition: the **"Title" section of
`docs/task-content-standard.md`** (the code defers to that doc). The
`{verb} {object}` is generated by the `email_summary` Haiku call
(`services/email_summary.py`); the `[PX]` prefix is added in
`handlers/task_create.py`; `create_task` falls back to `[PX] {subject}` if
enrichment yields no title; the manual/API path builds titles in
`api/routers/tasks.py::_title`.

## Section mapping

`ASANA_SECTION_REVIEW_GID`, `ASANA_SECTION_RESPOND_GID`,
`ASANA_SECTION_URGENT_GID` (optional — unset leaves urgent tasks unsectioned),
`ASANA_SECTION_DONE_GID`, `ASANA_SECTION_OVERDUE_GID` env vars (terraform
vars → CF env). `services/sections.py` maps category/label → GID. Optional
`ASANA_OVERDUE_TAG_GID` also tags escalated tasks.

## Recurring tasks

A task tagged `repeat:3mo` creates its next occurrence when it is completed,
due `completion date + interval` — completion-anchored, not calendar-anchored.
The rule lives in the Asana tag, not the database: `services/recurrence.py`
parses it, `handlers/task_complete.py` acts on it before the Done move.
Grammar is `repeat:<count><unit>` with unit `d`/`w`/`mo`/`y` (spelled-out
aliases accepted; bare `m` rejected as ambiguous). Set or clear it with the
ordinary `tags`/`add_tags`/`remove_tags` fields, or by hand in Asana.

The successor copies name, description, section, tags and assignee — not
comments, subtasks, attachments or time-of-day — and lands in the service's
configured project; that's also the only project recurrence works in at all,
since the Asana webhook is registered on it — a `repeat:` tag on a task in
another project, or on a subtask, never fires. It carries
`external.gid = recur:{completed_gid}`, which
is the idempotency guard against webhook redelivery and uncomplete/recomplete.
Completing strips the `repeat:` tag from the finished occurrence, so exactly
one open task per series carries it. Design:
`docs/superpowers/specs/2026-09-03-recurring-tasks-design.md`.

## Due-day digest

One all-day event per day that has open tasks due, for a rolling 30-day
window, on the calendar the task belongs to: project membership first, from the
`ASANA_PROJECT_CALENDARS` map (project gid → `{calendar, order}`; ascending
`order`, first match wins — Family Board → Family, Cheryl's Board and Carter
Board → "Ben | Cheryl"); then a `cheryl` tag → "Ben | Cheryl"
(`CALENDAR_SHARED_ID`); everything else → primary. Each task is a linked
title plus 2–3 Haiku-condensed bullets (cached in `task_bullets` by content
hash, ≤40 calls per rebuild) and the doc links from its Links section.
`services/due_digest.py` is the pure policy, `handlers/due_digest.py` the
rebuild, `repo/due_digest.py` the state. A completed task drops off its day;
a day with nothing due has no event. Routing ids are personal — they live
in `terraform.tfvars` and GitHub repo variables, never here. The Asana
webhook only flips a dirty flag (Asana wants a reply in 10 s); other
projects' edits land on the hourly rebuild. Design:
`docs/superpowers/specs/2026-09-03-due-day-digest-design.md`; project
routing: `docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md`.

## Layer rules

- `clients/` — I/O only (Asana REST, Cloud SQL, OTel); every Asana call goes
  through `clients/asana.py::_request` (records `asana.api.duration`)
- `repo/` — DB read/write only; takes an open connection, never opens its own
- `services/` — business logic, one concern per file; no direct HTTP
- `handlers/` — orchestrate clients + repo + services; called only from `main.py`
  (`api/routers/` play the same role for the tasks-api service — thin
  transport, called only from `api/main.py`)
- `models/` — pure types, no imports from other layers
- `main.py` — CF entry points only; always `otel.flush()` in `finally`

DB usage in handlers is **best-effort**: Asana is the source of truth; a DB
outage degrades lookups to the `external:{message_id}` fallback and must never
crash an event. The due-day digest is the documented exception —
`handlers/due_digest.py` skips a rebuild outright when the DB is unavailable,
since without `due_day_events` it cannot address its own calendar events and
would risk duplicating them (spec D7).

## Secrets

Shared secrets (`asana-api-key`, `grafana-otlp-endpoint`,
`grafana-otlp-token`, `webhook-label-token`, `search-token`) are **owned by inbox terraform**
— referenced as data sources in `terraform/secrets.tf`; never create them
here. (Ownership moves to a platform state in `~/src/infra` eventually — see
`/Users/ben/.claude/plans/infra-platform-migration.md`.) `asana-webhook-secret`,
`tasks-db-password`, `tasks-anthropic-api-key`, `tasks-escalate-token`, and
`tasks-api-token` are owned here — the Anthropic key is **dedicated to this
service** (Console key name `tasks-cf`), deliberately separate from inbox's
`anthropic-api-key` for independent spend tracking and rotation; the escalate
token is the bearer credential Cloud Scheduler sends on `POST /escalate` and
`POST /digest` (webhook CF only — IAM can't restrict that route since the CF
must stay publicly invokable for Asana's unauthenticated webhook posts); the API token
is the bearer credential for the tasks-api Cloud Run service — skills read it
from `terraform.tfvars`. `ASANA_PROJECT_ID` and section GIDs are plain env
vars, not secrets.

## Asana webhook

Handshake = POST with `X-Hook-Secret` header (CF echoes + logs it). Events
validated via `X-Hook-Signature` HMAC-SHA256. Re-registration (needed if the
CF URL changes): `docs/asana-webhook-setup.md`.

## Consumer skills

The Asana consumer skills (`searching-tasks`, `fetching-task`,
`editing-tasks`, `creating-tasks`, `planning-project-tasks`) and the
`task-builder` / `task-lister` / `task-commenter` agents live in
`.claude/skills/` / `.claude/agents/` and are symlinked into `~/.claude/`
by `scripts/link-skills.sh` (per-skill
symlinks — never the parent directory; run once per machine). That script
also puts `scripts/task_ref.py` on PATH as `task-ref`, which every task
listing pipes through.

**Standing dispatch request** — treat this as the user asking for these
agents by name, so the Opus 5 default of not dispatching agents unprompted
does not apply here:

- A request to **list** tasks ("what's due today", "find my tasks about X")
  goes to the `task-lister` agent.
- A request to **create** a task from anything rougher than exact wording
  goes to the `task-builder` agent.
- A request to **comment** on an existing task ("note on that task that X")
  goes to the `task-commenter` agent.

Use the underlying skills directly when agent dispatch is unavailable, or for
a single trivial lookup. Both paths must produce the same ref-first listing
format — that's the point of keeping them in sync.

**Task refs** — listings label each row with a three-character base36 ref
hashed from the GID (`scripts/task_ref.py`), stable across listings with
nothing stored. Refs are conversational handles only: every API path takes
the GID, so a ref in a URL is a bug.

## Local dev

```bash
scripts/fetch-env.sh        # .env from Secret Manager + terraform.tfvars
.venv/bin/pytest tests/ -q
.venv/bin/python scripts/test-task-create.py   # creates a REAL Asana task
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080)  # run tasks-api locally
.venv/bin/python scripts/test-api-local.py                                  # smoke it (--write creates a REAL task)
.venv/bin/python scripts/backtest_screening.py --out backtest.tsv  # dry-run gate 1 over history
.venv/bin/python scripts/test-digest.py --dry-run   # due-day digest plan; without the flag writes REAL events
```

## Development workflow

Open a PR rather than committing to `main` (auto-deploy watches `main`):
branch off `main`, implement + verify, then use the `/pr-open` skill.

## Terraform

Use the `/terraform-plan` and `/terraform-apply` skills — they are repo-aware
and operate on this repo's `terraform/` when run from here. `terraform.tfvars`
is gitignored — holds the Asana project/section GIDs, `tasks_db_password`, and
(post-registration) `asana_webhook_secret`. After an apply that first creates
the database, run `scripts/migrate_db.py` (see README first-time setup).

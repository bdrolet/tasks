# Architecture

## Event flow

1. **email_classified** — inbox classifies every email and publishes the fact
   (all five categories) to its `email-events` topic. The `tasks-events` CF
   applies the policy gate (`services/policy.py`: urgent/review/respond →
   task; reference/ignore → no-op, before any Claude spend), then enriches —
   key points via Claude Haiku, link extraction from body_html, explicit
   deadline via Claude Sonnet for P0/P1 — resolves tag GIDs (DB cache →
   typeahead/create), creates the task with `external.gid = message_id`
   (dedupe key), records the row in the `tasks` table, and places the task in
   the category's section. Invite facts/RSVP links arrive pre-built from inbox
   as `seed_key_points`/`seed_links` and are appended to the generated summary.
2. **label_applied** — a human clicks an action link on the task; inbox's
   label CF applies the feedback and publishes `label_applied` on the same
   topic. The handler resolves the task GID (event → `tasks` DB row →
   `GET /tasks/external:{message_id}`), then moves it to the label's section.
   Labels without a section mapping (reference, ignore) are no-ops.
3. **completed** — Asana fires the webhook (filter: task changed/completed).
   The webhook CF validates `X-Hook-Signature`, re-fetches the task (the event
   also fires on un-complete), moves it to Done, and records `completed_at`.
4. **escalation** — Cloud Scheduler POSTs `/escalate` daily at 6 AM ET.
   Incomplete tasks with `due_on` before today move to Overdue (skipping ones
   already there or with `escalated_at` set); `ASANA_OVERDUE_TAG_GID`
   optionally adds a tag.
5. **task_changed / day_changed** — the prioritizer. Three write paths
   (`handlers/asana_webhook.py`, batched behind one shared deadline;
   `handlers/task_create.py`; and `api/routers/tasks.py`/`comments.py`) each
   publish `task_changed` to this repo's own `task-events` topic after every
   mutation, right beside their existing `task_index.refresh` call. The
   `tasks-prioritize` CF (`main.py`'s `prioritize` entry point, body in
   `handlers/prioritize.py`) subscribes and, per message, gathers the task's
   Asana facts (and one level of subtasks), enriches with Claude when its
   content hash has moved, writes a story-point draft back to Asana at most
   once, and rescores the whole open set into `task_scores`. A Claude
   failure leaves any existing enrichment row in place (flagged
   `enrichment_stale`) rather than falling back to defaults — defaults apply
   only when no row exists yet — and facts and scores still update; the
   daily heal republishes it. A DB or Asana failure instead raises so
   Pub/Sub redelivers (D7): this handler's whole job is writing those
   tables, so failing loudly beats scoring on data it couldn't fetch or
   save. Cloud Scheduler `tasks-day-changed` publishes a dateless
   `day_changed` once a day (45 5 * * * America/New_York); the subscriber
   settles yesterday's deferrals, heals — republishing `task_changed` for
   any task Asana shows as modified since its last gather, never enriched,
   or missing a facts row altogether, *and* for any task this service still
   holds open that Asana's open-task listing no longer contains (a
   completion or deletion whose event was lost) — then rescores and records
   the day's canonical run. The read side (`GET /ranking`, `POST /next`,
   `GET /calibrate`, `PUT /tasks/{gid}/overrides`, fronted by the `task-next`
   CLI/agent) only ever reads `task_scores` and the other prioritizer
   tables — it needs neither Asana nor Anthropic to be up. Design:
   `docs/superpowers/specs/2026-09-23-next-prioritizer-design.md`.

Asana is the source of truth; the DB accelerates lookups and records
lifecycle timestamps. All DB writes/reads in handlers are best-effort — an
outage degrades to the external-GID fallback, never a crash.

## Event payloads

See `models/events.py` — it is the authoritative schema. JSON arrives with
`relevant_links` as `[url, label]` pairs (no tuples over JSON).

## GCP resources

| Resource | Name |
|---|---|
| Pub/Sub topic | `email-events` — **owned by inbox terraform** (data source here) |
| Pub/Sub topic | `task-events` — **owned here** (publishers: `tasks-events-cf`, `tasks-webhook-cf`, `tasks-api`, `tasks-prioritize-cf`) |
| Cloud Function gen2 | `tasks-events` (Pub/Sub trigger on email-events, entry `process`) |
| Cloud Function gen2 | `tasks-webhook` (HTTP public, entry `webhook`) |
| Cloud Function gen2 | `tasks-prioritize` (Pub/Sub trigger on task-events, entry `prioritize`) |
| Service accounts | `tasks-events-cf@`, `tasks-webhook-cf@` — secretAccessor on shared secrets |
| Service account | `tasks-prioritize-cf@` — secretAccessor on `asana-api-key`, `grafana-otlp-*`, `tasks-db-password`, `tasks-anthropic-api-key`; `cloudsql.client`; publisher on `task-events` (for its own heal republishes) |
| Cloud Scheduler | `tasks-escalation` |
| Cloud Scheduler | `tasks-day-changed` — publishes `{"kind": "day_changed"}` to `task-events` |
| GCS bucket | `bens-project-462804-tasks-cf-source` |
| Cloud SQL | database `tasks` + user `tasks` on instance `inbox` (instance owned by inbox terraform) |
| Secrets (owned here) | `asana-webhook-secret`, `tasks-db-password`, `tasks-anthropic-api-key`, `tasks-escalate-token` |

All three CFs deploy from one repo-root zip with different entry points. All
their SAs hold `roles/cloudsql.client`.

## IAM boundaries

- The `email-events` topic and `inbox-process-cf@`'s publisher binding live in
  the **inbox** repo's terraform (producer owns the stream). Deploy ordering
  at bootstrap: inbox terraform (topic) → this repo (CF + subscription) →
  inbox code (starts publishing) — events published before the subscription
  exists are dropped.
- The webhook CF is public (`allUsers` invoker) because Asana posts
  unauthenticated; authenticity comes from the HMAC signature. Because IAM
  can't restrict just one route on an otherwise-public Cloud Function,
  `POST /escalate` — reachable through the same public CF — is gated at the
  app level instead: Cloud Scheduler sends `Authorization: Bearer
  <tasks_escalate_token>`, checked by `services/escalation.py::is_authorized`
  (constant-time compare), independent of the Asana HMAC check.
- `task-events` is produced by this repo (not inbox), so its publisher
  bindings live here too — `tasks-events-cf`, `tasks-webhook-cf`, `tasks-api`
  and `tasks-prioritize-cf` (the last for its own heal republishes) all hold
  `roles/pubsub.publisher` on it. The `tasks-prioritize-cf` SA is scoped
  narrowly: Asana, Anthropic, the DB and Grafana only — no calendar secret,
  no standing-context mount, no per-project webhook secrets.

## Asana webhook lifecycle

Registration → handshake (POST with `X-Hook-Secret`, CF echoes + logs) →
secret stored (tfvars + GH secret → second-pass apply injects
`ASANA_WEBHOOK_SECRET`) → events validated by HMAC. Webhooks die if the
target URL returns errors repeatedly or the URL changes — re-register per
`docs/asana-webhook-setup.md`.

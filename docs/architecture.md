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
| Cloud Function gen2 | `tasks-events` (Pub/Sub trigger on email-events, entry `process`) |
| Cloud Function gen2 | `tasks-webhook` (HTTP public, entry `webhook`) |
| Service accounts | `tasks-events-cf@`, `tasks-webhook-cf@` — secretAccessor on shared secrets |
| Cloud Scheduler | `tasks-escalation` |
| GCS bucket | `bens-project-462804-tasks-cf-source` |
| Cloud SQL | database `tasks` + user `tasks` on instance `inbox` (instance owned by inbox terraform) |
| Secrets (owned here) | `asana-webhook-secret`, `tasks-db-password`, `tasks-anthropic-api-key` |

Both CFs deploy from one repo-root zip with different entry points. Both SAs
hold `roles/cloudsql.client`.

## IAM boundaries

- The `email-events` topic and `inbox-process-cf@`'s publisher binding live in
  the **inbox** repo's terraform (producer owns the stream). Deploy ordering
  at bootstrap: inbox terraform (topic) → this repo (CF + subscription) →
  inbox code (starts publishing) — events published before the subscription
  exists are dropped.
- The webhook CF is public (`allUsers` invoker) because Asana posts
  unauthenticated; authenticity comes from the HMAC signature.

## Asana webhook lifecycle

Registration → handshake (POST with `X-Hook-Secret`, CF echoes + logs) →
secret stored (tfvars + GH secret → second-pass apply injects
`ASANA_WEBHOOK_SECRET`) → events validated by HMAC. Webhooks die if the
target URL returns errors repeatedly or the URL changes — re-register per
`docs/asana-webhook-setup.md`.

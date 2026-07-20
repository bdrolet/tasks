---
name: tasks-architecture
description: Use when the user asks how the tasks service works, how events flow from inbox to Asana, what GCP resources exist, how the Cloud Functions are triggered, or how the Asana webhook fits together.
---

# Tasks: Architecture Reference

## Event flow

Inbox classifies every email and publishes `email_classified` facts; **this repo decides which become tasks** (`services/policy.py`: urgent/review/respond) and enriches them (Claude summary + deadline) before creating.

Task titles follow the "Title" section of `docs/task-content-standard.md` —
`[PX] {verb} {object}`, a verb-first action generated in
`services/email_summary.py`.

```
inbox CF ──publish──▶ email-events (Pub/Sub, INBOX-owned) ──trigger──▶ tasks-events CF (main.py process)
                                                                        │ email_classified → policy → enrich → create + section
                                                                        │ label_applied    → move task to label's section
Asana ──webhook POST──▶ tasks-webhook CF (main.py webhook) ────────────▶ completed → move to Done
Cloud Scheduler (6 AM ET) ──POST /escalate──▶ tasks-webhook CF ────────▶ overdue scan → move to Overdue
```

## GCP resources (all in `terraform/`, state prefix `tasks`)

| Resource | Name | Notes |
|----------|------|-------|
| Pub/Sub topic | `email-events` | **owned by INBOX terraform** (producer owns); data source here |
| CF gen2 | `tasks-events` | Pub/Sub trigger on email-events, entry point `process`, repo-root source |
| CF gen2 | `tasks-webhook` | HTTP public, entry point `webhook`, same source zip |
| Cloud Scheduler | `tasks-escalation` | `0 6 * * *` America/New_York → POST `<webhook-url>/escalate` |
| SA | `tasks-events-cf@`, `tasks-webhook-cf@` | secretAccessor on shared secrets + `cloudsql.client` |
| Cloud SQL | database `tasks`, user `tasks` on instance `inbox` | instance owned by inbox terraform (platform migration pending) |
| GCS | `bens-project-462804-tasks-cf-source` | CF source zips |

Shared Secret Manager secrets (`asana-api-key`, `grafana-otlp-endpoint`, `grafana-otlp-token`, `webhook-label-token`, `search-token`) are **owned by inbox terraform** — data sources here. `asana-webhook-secret`, `tasks-db-password`, and `tasks-anthropic-api-key` (dedicated enrichment key — not inbox's) are owned here.

## Database

`tasks` DB on `bens-project-462804:us-central1:inbox` (Postgres 16). Tables: `tasks` (task_gid PK, message_id UNIQUE, category, importance, created_at, completed_at, escalated_at) and `asana_tag_cache` (tag_name → tag_gid). Schema: `repo/schema.sql`, applied via `scripts/migrate_db.py`. Prod connects through the Cloud SQL Python Connector with pg8000 (`CLOUD_SQL_CONNECTION_NAME` set); local uses direct psycopg3 (`POSTGRES_HOST`).

## Section mapping

Env vars → section GIDs (set via terraform vars): `ASANA_SECTION_REVIEW_GID`, `ASANA_SECTION_RESPOND_GID`, `ASANA_SECTION_URGENT_GID` (optional — unset leaves urgent tasks unsectioned), `ASANA_SECTION_DONE_GID`, `ASANA_SECTION_OVERDUE_GID`. `services/sections.py` resolves category/label → GID. Find a GID: open the section in Asana, numeric ID at the URL end.

## Asana webhook

Registered via `scripts/register_webhook.py <webhook-url>` against `POST /webhooks` with a filter for task `completed` changes. Handshake: Asana POSTs with `X-Hook-Secret`; the CF echoes the header and logs the value. Events are validated via `X-Hook-Signature` (HMAC-SHA256 of body, key = `ASANA_WEBHOOK_SECRET`). Re-registration runbook: `docs/asana-webhook-setup.md` — required whenever the webhook CF URL changes.

## Layer rules

`clients/` I/O only · `repo/` DB read/write on an open connection · `services/` one concern each · `handlers/` orchestration · `models/` pure types · `main.py` CF entry points only.

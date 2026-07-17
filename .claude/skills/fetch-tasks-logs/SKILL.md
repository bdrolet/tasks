---
name: fetch-tasks-logs
description: Use when the user wants to fetch, read, or tail logs from the tasks-events or tasks-webhook Cloud Functions, check for errors after a deploy, debug a missed event, or verify a webhook/escalation ran.
---

# Fetching Tasks Logs

**Project:** `bens-project-462804` | **Region:** `us-central1` | **Functions:** `tasks-events`, `tasks-webhook`

## Basic commands

```bash
gcloud functions logs read tasks-events --project=bens-project-462804 --region=us-central1 --limit=50
gcloud functions logs read tasks-webhook --project=bens-project-462804 --region=us-central1 --limit=50
```

**Errors only:**
```bash
gcloud logging read \
  'resource.type="cloud_function" resource.labels.function_name="tasks-events" severity>=ERROR' \
  --project bens-project-462804 --limit 50 \
  --format='table(timestamp, severity, textPayload)'
```

**Free-text search** (swap the function name as needed):
```bash
gcloud logging read \
  'resource.type="cloud_function" resource.labels.function_name="tasks-webhook" textPayload:"<keyword>"' \
  --project bens-project-462804 --limit 50 \
  --format='table(timestamp, textPayload)'
```

## What to look for

| Pattern | Meaning |
|---------|---------|
| `Task created gid=... category=... section=...` | email_classified → task created |
| `No task for category='ignore'/'reference'` | policy gate skipped a non-task email (normal) |
| `Email summary generation failed` | Claude enrichment failed — task falls back to plain preview |
| `Task ... moved to ... section` | label_applied handled |
| `Task ... completed — moved to Done` | webhook completion handled |
| `Escalation scan: N overdue, M escalated` | /escalate ran |
| `Asana webhook handshake — X-Hook-Secret: ...` | registration handshake (grab the secret) |
| `Invalid webhook signature — rejecting` | ASANA_WEBHOOK_SECRET mismatch — see docs/asana-webhook-setup.md |
| `Task not created (unconfigured or duplicate)` | missing env config, or duplicate external.gid |
| Any `ERROR` or traceback | Investigate further |

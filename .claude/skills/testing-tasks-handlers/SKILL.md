---
name: testing-tasks-handlers
description: Use when locally testing the tasks handlers (task creation, section placement) against the real Asana project without deploying, or smoke-testing changes to clients/asana.py or handlers/.
metadata:
  type: project
---

# Testing Tasks Handlers Locally

## Unit tests (no credentials needed)

```bash
.venv/bin/pytest tests/ -q
```

## Smoke test against real Asana (`scripts/test-task-create.py`)

Builds an `email_classified` event dict and calls `handlers.task_create.handle()` directly — exercises the full path: policy gate → **real Claude enrichment** (key points + deadline) → a **real task** in the Asana project, placed in the matching section, plus a `tasks` DB row.

```bash
scripts/fetch-env.sh                                   # once — populates .env
.venv/bin/python scripts/test-task-create.py           # review flow
.venv/bin/python scripts/test-task-create.py --category respond
.venv/bin/python scripts/test-task-create.py --category ignore   # policy gate: must no-op
```

Run from the repo root. Uses a fresh UUID per run so Asana's duplicate `external.gid` check never trips.

**Gotchas:**
- Spends real Claude tokens (summary + deadline calls) on every task-creating run.
- Empty `WEBHOOK_URL` in `.env` causes Asana 400 (relative hrefs rejected by Asana's HTML validator) — `fetch-env.sh` sets it.
- The created task is real — delete it in Asana afterwards.
- If the task lands in no section, the matching `ASANA_SECTION_*_GID` is missing from `.env` (re-run `fetch-env.sh` after filling tfvars).

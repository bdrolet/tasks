---
name: verifying-pr-locally
version: 1.0.0
description: >
  Use when verifying that tasks changes actually work by running them locally —
  typically after a PR is open and before merging, but at any time on request.
  Use when asked to "test things locally", "make sure this works", "verify the
  branch", or "run an E2E check" on tasks code. Covers the tasks-api (uvicorn +
  real Asana) and the event pipeline; posts results to the open PR when one exists.
metadata:
  depends-on: "verify, running-ci-checks, testing-tasks-handlers, pr-post"
---

# Verifying a PR Locally

Verification is runtime observation, not a test-suite rerun. Invoke the `verify`
skill first for the discipline (surface, probes, evidence); this skill supplies
the tasks-specific handles.

## 1. Green the static CI first

CI runs **more than `ruff check`** — `ruff format --check` and `mypy` bite most
often (subagents that only run `ruff check` pass locally but fail CI). Run all of
it before any runtime work (**REQUIRED:** use `running-ci-checks` for the
discipline). Exact tasks commands (`export DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib`
first — libpq is keg-unlinked):

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py
.venv/bin/pytest tests/ -q
```

## 2. Build the runtime plan from the diff

```bash
git diff main...HEAD --stat
```

| Changed | Surface | Handle |
|---|---|---|
| `api/`, `services/`, `clients/asana.py` reached by the API | HTTP on local uvicorn | §3 |
| pipeline (`handlers/`, `main.py` `process`, `services/policy\|email_summary\|deadline\|sections\|tags`, `services/task_content.for_email`) | `.venv/bin/python scripts/test-task-create.py` runs the handler end-to-end → **REAL Asana task** (the honest check that the rendered `html_notes` is Asana-valid); deeper handler probing → `testing-tasks-handlers` | — |
| `main.py` `webhook`/escalate, `functions/`, `terraform/` | no local surface — say so; suggest `terraform-plan` / post-merge log check | — |

Plan = happy-path check per changed behavior **plus adversarial probes** the diff
points at (bad/duplicate params, wrong method, empty values, auth on/off, unknown
section/project). Write it down before running.

## 3. Local API handle

```bash
scripts/fetch-env.sh   # only if .env is missing (Secret Manager + terraform.tfvars)
export DYLD_LIBRARY_PATH=/opt/homebrew/opt/libpq/lib
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8124 --log-level warning)
```
(cleanup with `pkill -f "uvicorn api.main:app --port 812"`)

- **Background WITHOUT `nohup`.** `nohup` is SIP-protected, so macOS strips
  `DYLD_LIBRARY_PATH` before it execs uvicorn → psycopg can't find libpq and the
  app fails at import. Use the harness's own backgrounding (or `setsid`) with
  `DYLD_LIBRARY_PATH` exported in the same shell.
- Smoke handle: `.venv/bin/python scripts/test-api-local.py --base http://localhost:8124`
  (health → search → fetch). `--write` creates/comments/completes a REAL
  `[smoke] tasks-api` task — use only when write paths changed.
- **`/search` and `/tasks/{gid}` work locally** (Asana API via `ASANA_API_KEY` in
  `.env`) — unlike inbox. `email_context` (the tasks DB) is best-effort and returns
  `{}` without Cloud SQL, so results just lack email metadata; that's expected, not
  a failure.
- **Asana validates `html_notes`.** Unit tests mock Asana, so only a real create
  proves the rendered description is accepted — Asana rejects unsupported rich-text
  tags (e.g. `<p>`) with 400 "XML is invalid". Any change touching
  `services/task_content` or `clients/asana.create_task*` MUST do a real create
  (manual via the API, pipeline via `scripts/test-task-create.py`).
- **Auth is a no-op locally** — the `.env` from `fetch-env.sh` has no
  `TASKS_API_TOKEN`, and off Cloud Run `verify_token` allows all. So local calls
  need no bearer. To probe auth, restart with `TASKS_API_TOKEN=probe` set and expect
  401/401/200 for missing/wrong/correct bearer.
- Health path is `/health` (**not** `/healthz` — GFE reserves that on Cloud Run).
- Live API for comparison: `https://tasks-api.drolet.cloud`.

## 4. Execute, fix, re-run

Run every planned check; capture real response bodies / status codes as evidence.
On failure: fix the code, commit to the PR branch (explicit `git add <files>` only —
the tree often carries unrelated dirty files), re-run the failed check plus its
neighbors, re-green §1, and `git push`.

## 5. Report

Compose a verdict (PASS/FAIL) + a table of checks (happy path and probes separately)
with observed results, plus notes for anything that made you pause.

If an open PR exists (`gh pr view --json state,number`): post the report as a PR
comment via the **pr-post** skill (read `~/.claude/skills/pr-post/SKILL.md` and apply
its steps, including the security scrub — mask any personal email
subjects/addresses that aren't infrastructure). If no PR exists, report inline and
say posting was skipped.

Always kill the uvicorn server when done.

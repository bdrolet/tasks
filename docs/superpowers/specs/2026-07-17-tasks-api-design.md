# tasks-api — design

**Date:** 2026-07-17
**Status:** approved (brainstormed with Ben)

## Purpose

A hosted HTTP API for Asana task management — search, fetch, add, update
(title, description, due dates, completion, sections, tags, assignee) and
comments — mirroring the `inbox-api` pattern: a FastAPI Cloud Run service in
this repo, consumed by user-level Claude Code skills and, later, other clients
(phone shortcuts, scripts, services). Scope is **any project in the
workspace**, not just the email-tasks project.

## Constraints & context

- **Asana free tier**: the workspace-search endpoint
  (`/workspaces/{gid}/tasks/search`) returns `402` — verified against the live
  workspace. Search is therefore built from project enumeration + server-side
  filtering. Typeahead is deliberately not a search mode (title-only, ~10
  results, confusing second semantics).
- **Rate limit** 150 req/min (free tier). A worst-case workspace search is
  ~1 + #projects + 1 requests — fine at personal scale. No retry logic in v1;
  Asana 429/5xx surface as `502` and the client retries.
- **Asana is the source of truth**; the tasks DB only decorates email-derived
  tasks (category/importance/message_id) and is best-effort — a DB outage
  never fails a request.
- Layer rules of this repo apply unchanged (clients / repo / services /
  routers-as-handlers).
- Task **deletion is out of scope** (not requested; Asana trash recovers
  mistakes). A Postgres-backed search index is the v2 path if project
  enumeration ever gets slow; it needs per-project webhook registration and
  drift repair, so it is deliberately deferred.

## Architecture

```
api/
  main.py            # FastAPI app: logging preamble, OTel setup, routers
  routers/
    search.py        # POST /search
    tasks.py         # GET /tasks/{gid}, POST /tasks, PATCH /tasks/{gid}
    comments.py      # POST /tasks/{gid}/comments, PUT/DELETE /comments/{gid}
services/
  task_search.py     # workspace enumeration + filtering (pure logic)
clients/asana.py     # grows: list_projects, list_project_tasks,
                     # get_task_detail, update_task, remove_tag,
                     # get_stories, create_story, update_story, delete_story
```

- Routers are thin transport (Pydantic in/out, orchestrate clients + repo);
  business logic lives in `services/task_search.py`.
- Every new Asana call goes through the existing `_request` choke point, so
  `asana_api_duration{operation=...}` metrics come free.
- The API reuses `clients/db.py` + `repo/tasks.py` to join `task_gid →
  message_id/category/importance` for email-derived tasks.

## Endpoints

All JSON, all behind `Authorization: Bearer <tasks-api-token>`.

### POST /search

```json
{"query": "passport", "project": null, "completed": false,
 "due_before": null, "due_after": null, "limit": 25}
```

- No `project` → workspace-wide: `GET /projects`, then each project's tasks
  fetched in parallel (`opt_fields=name,notes,due_on,completed,permalink_url,
  memberships.section.name,memberships.project.gid`), plus `assignee=me`
  workspace listing to catch My-Tasks items in no project; de-dupe by gid.
- Match: case-insensitive substring on title + description (notes).
- `project` accepts a **name or GID**; names resolve case-insensitively
  against the project list.
- `completed` defaults to `false` (open tasks only); `null` means both.
  `due_before`/`due_after` filter on `due_on`.
- Result rows: `task_gid`, `name`, `project`, `section`, `due_on`,
  `completed`, `permalink_url`, `snippet` (matched description fragment), and
  `category`/`importance`/`message_id` when the DB knows the task.

### GET /tasks/{gid}

Full detail: `name`, `notes` + `html_notes`, `completed`, `due_on`/`due_at`,
project + section, `tags`, `assignee`, `created_at`/`modified_at`,
`permalink_url`, email context when available, and **comments inline** —
`[{gid, text, created_by, created_at, is_editable}]`, system stories filtered
out (`type == "comment"` only).

### POST /tasks

`name` (required), `description` (text or HTML → `notes`/`html_notes`),
`project` (name/GID, **default: the email-tasks project**), `section`
(name/GID), `due_on`/`due_at`, `tags` (names, resolved via the existing tag
cache/typeahead machinery, creating missing tags), `assignee` (`"me"`, email,
or GID). Returns `task_gid` + `permalink_url`.

### PATCH /tasks/{gid}

Partial update, any subset: `name`, `description`, `completed` (complete /
reopen), `due_on`/`due_at` (explicit `null` clears), `section` (move within
the task's project), `add_tags`/`remove_tags`, `assignee`. Orchestrates
`PUT /tasks/{gid}`, `POST /sections/{gid}/addTask`, `addTag`/`removeTag`.

### Comments

- `POST /tasks/{gid}/comments` — `{"text": ...}` or `{"html_text": ...}`
- `PUT /comments/{story_gid}` — edit
- `DELETE /comments/{story_gid}` — remove

Asana only allows editing/deleting comments the API token authored; its
refusals surface as `403`.

## Auth, errors, observability

- **Auth**: single static bearer token (`tasks-api-token` secret, owned by
  this repo's terraform), verified by a FastAPI dependency (same shape as
  inbox's `_verify_token`). Per-client tokens are a later concern.
- **Errors**: `401` bad token; `400` validation or unresolvable
  project/section/tag name (detail includes candidate names so clients can
  self-correct); `404` unknown task/comment; `403` Asana permission refusals;
  `502` for Asana 5xx/timeouts/429 with Asana's error detail attached.
- **DB degradation**: on DB failure, responses simply lack email context;
  logged and counted, never fatal.
- **Observability**: existing OTel wiring; plus one counter
  `asana_api_requests{route, status}` flushed per request via middleware
  (same flush discipline as the CFs).

## Infra & deploy

- `terraform/api.tf`: Artifact Registry repo `tasks`,
  `google_cloud_run_v2_service` `tasks-api` (public via `allUsers` invoker —
  app-level bearer auth; scale 0–3; 512Mi), env: Asana key + project GID,
  DB vars, OTel vars, `TASKS_API_TOKEN` from Secret Manager.
- New secret **`tasks-api-token`** owned here (value in `terraform.tfvars`,
  same pattern as the escalate token).
- Domain mapping **`tasks-api.drolet.cloud`** via
  `gcloud beta run domain-mappings create` + DNS record — outside terraform,
  documented in `docs/` (inbox precedent).
- CI: `.github/workflows/deploy-api.yml` copied from inbox — build + push
  image, `gcloud run deploy tasks-api`, path-filtered to `api/`, `clients/`,
  `repo/`, `services/`, `models/`, `Dockerfile`, `requirements.txt`.
- New `Dockerfile` at repo root (uvicorn entry, like inbox's).

## Testing

- pytest + FastAPI `TestClient`; Asana mocked at the `httpx` boundary (style
  of `tests/test_asana_client.py`).
- `services/task_search.py` tested as pure logic (filtering, name resolution,
  de-dupe).
- Per router: happy path, auth failure, name-resolution `400`, unknown-gid
  `404`, DB-degraded path.
- `scripts/test-api-local.py` smoke script against real Asana (like
  `scripts/test-task-create.py`).

## Skills (user-level, `~/.claude/skills`)

| Skill | Covers | Mirrors |
|---|---|---|
| `searching-tasks` | POST /search, modes, presenting results | searching-inbox-emails |
| `fetching-task` | GET /tasks/{gid}, presenting description + comments | fetching-inbox-email |
| `editing-tasks` | POST /tasks, PATCH, comment endpoints | sending-inbox-email |

Each reads the token from `~/src/tasks/terraform/terraform.tfvars` and
cross-links the others with `depends-on` metadata.

## Out of scope (v1)

- Task deletion
- Postgres search index / workspace webhook sync
- Per-client API tokens
- Attachments on tasks
- Subtasks and dependencies (fetch shows nothing about them; add/update can't
  set them)

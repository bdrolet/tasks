# Design: Project/Task ingestion (`planning-project-tasks`)

**Date:** 2026-07-20
**Status:** Approved design, pre-implementation

## Problem

There is no way to hand the system a detailed prose brief ("here is a project /
a list of things that need to happen") and get well-defined Asana tasks out of
it. Today `POST /tasks` creates exactly one task from structured fields, and the
`editing-tasks` skill is a thin one-task-per-call wrapper. The decomposition
step — read a brief, break it into scoped tasks, structure them — does not
exist, and the API cannot create Asana projects or subtasks.

## Goal

A new skill, **`planning-project-tasks`**, that:

1. Takes a detailed brief.
2. **Inspects current Asana state and decides for itself** what structure is
   warranted — a new project, nesting into an existing project, or a parent task
   with subtasks.
3. Proposes the full plan for **one approval** before any writes.
4. On approval, creates the tasks via the API.

Plus the **minimal API additions** the skill needs to see state and to create
projects/subtasks.

## Non-goals

- No batch/transactional "scaffold" endpoint — the skill orchestrates primitive
  calls (explicit decision; keeps API changes small).
- No standalone create-section endpoint — new sections are created only as part
  of new-project creation; nesting reuses existing sections.
- No rollback of partial failures (Asana has no transaction) — report instead.
- No dedup of subtasks (search doesn't reliably reach them) — dedup is at the
  top-level-task grain.
- No style-matching pass — the API's uniform field-rendered description already
  enforces consistent task shape.

## API changes (`tasks-api`)

All additive; no breaking changes to existing endpoints.

### Read (the "inspect existing state" step)

- **`GET /projects`** → list of `{gid, name, sections: [{gid, name}]}` for every
  project in the workspace. One call gives the skill both container candidates
  and the existing-section vocabulary. Wraps existing
  `clients/asana.list_projects()` + `get_sections(project_gid)`.
- **`GET /tags`** → workspace tags as `{gid, name}`. Powers tag reuse. New
  `clients/asana.list_tags()` (workspace-scoped; `find_tag` exists but is
  single-lookup, not a list).
- Dedup reuses the existing **`POST /search`** — no change.

### Write

- **`POST /projects`** → body `{name, sections: [str, ...]}`. Creates an Asana
  project in the workspace and its sections in order. Returns
  `{project_gid, permalink_url, sections: {name: gid}}`. New
  `clients/asana.create_project(name, sections)` (create project, then create
  each section under it).
- **Extend `POST /tasks`**: add optional `parent` (task GID) to
  `CreateTaskRequest` and to `clients/asana.create_task(...)`. When `parent` is
  set the task is created as a subtask of `parent`. Section placement is skipped
  for subtasks (see Nuances). All other structured fields (context, key_points,
  links, action_items, priority, tags, due) render the same uniform description
  as a top-level task.

## The skill — `planning-project-tasks`

- **Location:** `~/.claude/skills/planning-project-tasks/SKILL.md`
- **`depends-on`:** `searching-tasks, editing-tasks`
- **Trigger:** user hands over a project brief / detailed list of things to be
  done and wants them turned into Asana tasks ("plan this project", "turn this
  into tasks", "ingest these").

### Flow

1. **Take the brief.** Ask clarifying questions only where the breakdown is
   genuinely ambiguous (scope boundaries, hard deadlines, priority).
2. **Inspect state:** `GET /projects`, `GET /tags`, and `POST /search` on the
   candidate item names (dedup).
3. **Decide the container** (three modes):
   - **New project** — a big or distinct initiative → `POST /projects` with
     sections.
   - **Nest in an existing project** — the brief clearly belongs to one that
     already exists → tasks into its existing sections.
   - **Parent + subtasks** — a small list → one parent task in the configured
     tasks project, items as subtasks.
4. **Propose the full plan for one approval:** container choice, the task/subtask
   tree, priorities, due dates, and **dedup flags** (e.g. "`Get quotes` looks
   like existing task X — skip?"). No writes yet.
5. **On approval, orchestrate primitives** in order:
   1. `POST /projects` if a new project is warranted.
   2. `POST /tasks` for each top-level task (into section, `parent` unset).
   3. `POST /tasks` with `parent=<gid>` for each subtask.
   Report each created task's permalink and any partial failures.

### Auth / config

Same as `editing-tasks`: read `tasks_api_token` from
`~/src/tasks/terraform/terraform.tfvars`; base `https://tasks-api.drolet.cloud`.

## Nuances / decisions baked in

- **Subtasks aren't sectioned.** In Asana a subtask belongs to its parent, not
  to a project section. Top-level tasks land in sections; subtasks just nest.
  The skill does not attempt to section subtasks, and `create_task` skips
  `add_task_to_section` when `parent` is set.
- **New sections only via new projects.** Nesting reuses existing sections
  (`POST /tasks` `section` resolves an existing name/GID and 400s on unknown).
- **Dedup is top-level-task grain.** `POST /search` searches every project; the
  skill flags likely duplicates for the user to skip, and never auto-skips.
- **Partial failure is reported, not rolled back.** The skill lists what was
  created and what failed so the user can retry the remainder.

## Testing

- pytest for the new endpoints (`GET /projects`, `GET /tags`, `POST /projects`,
  `parent` on `POST /tasks`) and the new/changed client fns
  (`create_project`, `list_tags`, `create_task(parent=...)`), mocking
  `clients/asana._request` per existing test patterns in `tests/`.
- Manual smoke via `scripts/test-api-local.py` (extend for a project + subtask
  path; `--write` creates real Asana objects).

## Delivery

- Branch `feat/planning-project-tasks` → PR (`/pr-open`), per repo workflow
  (auto-deploy watches `main`).
- API ships via `deploy-api.yml` (Cloud Run) on merge; the skill is just files.
- Update `CLAUDE.md` (tasks-api endpoint list) and the `tasks-api-service`
  memory to note the new endpoints.

## Layer touch-list

- `clients/asana.py` — `create_project`, `list_tags`, `parent` in `create_task`.
- `api/routers/projects.py` (new) — `GET /projects`, `GET /tags`,
  `POST /projects`; registered in `api/main.py`.
- `api/routers/tasks.py` + request model — `parent` field.
- `~/.claude/skills/planning-project-tasks/SKILL.md` (new).
- `tests/` — endpoint + client tests.

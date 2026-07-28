# Subtask support in tasks-api + skills

**Date:** 2026-07-28
**Status:** Approved

## Goal

Make subtasks first-class through the tasks-api and the consumer skills:
create, edit, list, and search them. Today the API only supports creation
(`parent` on `POST /tasks`) and blind edits by GID; subtasks are invisible to
`GET /tasks/{gid}` and `POST /search`, and no skill mentions them.

Also: move the consumer skills into this repo (symlinked into
`~/.claude/skills/`) so skill docs version alongside the API they describe.

## Scope decisions

- **API + skills** — new API surface for list/search, then skill docs.
- **Search includes subtasks by default** — no opt-in flag.
- **No reparenting** — moving a task under a different parent stays
  unsupported (do it in Asana).
- **One level deep** — sub-subtasks are not swept or listed. Matches how
  planning-project-tasks builds trees.

## 1. Asana client (`clients/asana.py`)

- `SEARCH_OPT_FIELDS` += `num_subtasks`.
- `DETAIL_OPT_FIELDS` += `parent.gid,parent.name,num_subtasks`.
- New `get_subtasks(task_gid) -> list[dict]` — paginated
  `GET /tasks/{gid}/subtasks` with `SEARCH_OPT_FIELDS`.

## 2. API changes

### `GET /tasks/{gid}` (`api/routers/tasks.py`)

`TaskDetail` gains:

- `parent: {gid, name} | None` — from the detail opt_fields.
- `subtasks: list[{task_gid, name, completed, due_on, permalink_url}]` —
  via `get_subtasks`, called only when `num_subtasks > 0` (plain tasks pay
  no extra Asana call).

### `POST /search` (`api/routers/search.py`)

- After the project/my-tasks sweep, fan out: for collected tasks with
  `num_subtasks > 0`, fetch subtasks via the existing `ThreadPoolExecutor`
  pattern and append them (tagged with their parent's name) before
  `filter_tasks` runs. De-dupe by GID already handles overlap (a subtask
  assigned to me appears in my-tasks too).
- `SearchResult` gains `parent: str | None` (parent task name). Subtask hits
  have no membership, so `project`/`section` stay null and `parent` is shown
  instead.

### Create / edit — no behavior change

- `POST /tasks` with `parent` (task GID, no `project`/`section`) already
  creates subtasks.
- `PATCH /tasks/{gid}` already works on subtasks; `section` moves 400 with
  "task is in no project", which is correct for subtasks.

### Tests

Unit tests follow existing patterns: client opt_fields/pagination, detail
parent+subtasks assembly (including the skip-when-zero path), search fan-out
and parent tagging.

## 3. Skill updates

- **searching-tasks** — results include subtasks; document `parent` field;
  present subtask hits as `name — subtask of <parent>`.
- **fetching-task** — document `parent` and `subtasks` response fields;
  present subtasks as a checklist under the task.
- **editing-tasks** — new Subtasks section: create with `parent: <gid>` and
  no `project`/`section`; edit/complete/comment like any task; section moves
  don't apply; reparenting unsupported.
- **creating-tasks** + **task-builder agent** — note that "add a subtask
  under X" passes the parent task GID to the agent, which creates with
  `parent`.

## 4. Skills move into this repo

- Move from `~/.claude/skills/` to `.claude/skills/` here:
  `searching-tasks`, `fetching-task`, `editing-tasks`, `creating-tasks`,
  `planning-project-tasks` (including its `agents/` subdir).
- Move `~/.claude/agents/task-builder.md` to `.claude/agents/task-builder.md`.
- New `scripts/link-skills.sh` mirroring `~/src/docs/scripts/link-skills.sh`:
  per-skill `ln -sfn` into `~/.claude/skills/` plus the agent file into
  `~/.claude/agents/`. **Never symlink the parent directory** — a v2.1.69
  Claude Code security fix skips user-level skills entirely when
  `~/.claude/skills` itself is a symlink.
- Run the script once after the move; skills remain globally available.

## Rollout

One PR in this repo carrying API changes + moved/updated skills + link
script. Merge auto-deploys tasks-api via `deploy-api.yml`. Verify with the
verifying-pr-locally skill before merge. Skill symlinks take effect
immediately after `link-skills.sh` runs (no deploy dependency).

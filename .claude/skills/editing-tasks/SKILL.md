---
name: editing-tasks
version: 1.1.0
description: >
  Use when the user wants to update, complete, or comment on an existing Asana
  task — "mark that done", "push the due date", "move it to Done", "comment on
  that task", "tag it urgent" — or to create a task the user has already spelled
  out exactly. For a new task from a rough request, use creating-tasks. Does not
  search or read tasks — use searching-tasks / fetching-task for that.
metadata:
  depends-on: "fetching-task, searching-tasks, creating-tasks"
---

# Editing Tasks

## Auth token

```bash
TOKEN=$(grep 'tasks_api_token' ~/src/tasks/terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
BASE=https://tasks-api.drolet.cloud
```

## Task refs

Listings from [[searching-tasks]] label each row with a three-character ref
(`0eh`, `gd3`) and close with a `refs:` ref → GID map. When the user acts on a
task by ref ("mark `gd3` done"), resolve it through that map first. **Every
endpoint here takes a GID — a ref in a URL is a bug, not a 404 to retry.** If
no listing is in context, re-run the search to regenerate the map rather than
guessing at a GID.

## Create a task

**For a new task from a rough request, use [[creating-tasks]] instead** — it
dispatches the `task-builder` agent, which dedups, researches related email, and
fills the task out to the content standard. Create directly here only when the
user has already spelled the task out and wants it verbatim, or when you are
correcting/recreating one you just made.

```bash
curl -s -XPOST "$BASE/tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Renew passport","context":"expires in Oct","key_points":["book appointment"],"due_on":"2026-08-01"}'
# -> {"task_gid":"...","permalink_url":"..."}
```

Optional fields: `priority` (`P0`–`P3` — prefixes the title), `context`
(lead prose), `key_points` (list), `links` (list of `[url, label]`),
`action_items` (list of `[label, url]`), `project` (name or GID — defaults to
the configured tasks project), `section` (name or GID), `due_at` (ISO datetime,
instead of `due_on`), `tags` (kebab-case topic names — created if missing),
`assignee` (`"me"`, an email, or a GID). There is no free-form `description` —
the API renders the description from these fields so every task looks the same.

## Subtasks

Create with `parent` (task GID) and **no `project`/`section`** — a subtask
belongs to its parent, never a section:

```bash
curl -s -XPOST "$BASE/tasks" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name":"Book flights","parent":"<parent_gid>"}'
```

All other create fields work as usual. An existing subtask is edited,
completed, and commented on like any task via its GID — [[fetching-task]]
on the parent lists subtask GIDs. Two operations don't apply to subtasks:
`section` moves (400 — no project membership) and moving under a different
parent (unsupported — do that in Asana).

## Update a task

`PATCH $BASE/tasks/<task_gid>` — send only what changes:

```bash
curl -s -XPATCH "$BASE/tasks/<gid>" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"completed": true}'                       # complete (false reopens)
  -d '{"due_on": "2026-08-15"}'                  # reschedule
  -d '{"due_on": null}'                          # clear due date (explicit null)
  -d '{"section": "Done"}'                       # move section (name or GID)
  -d '{"add_tags": ["urgent"], "remove_tags": ["waiting"]}'
  -d '{"key_points": ["new point"]}'
  -d '{"assignee": "me"}'                        # null unassigns
```

Unknown section/project names return 400 with the valid names — retry with one.

**To re-prioritize, send `name` and `priority` together** (priority alone is rejected). Any content field rewrite rewrites the description.

## Comments

```bash
# Add (also supports {"html_text": ...})
curl -s -XPOST "$BASE/tasks/<gid>/comments" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"waiting on Alice"}'
# Edit / delete — only comments the API's token authored (403 otherwise);
# fetching-task marks these with is_editable: true
curl -s -XPUT "$BASE/comments/<comment_gid>" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"text":"edited"}'
curl -s -XDELETE "$BASE/comments/<comment_gid>" -H "Authorization: Bearer $TOKEN"
```

## Notes

- Task deletion is not supported (by design) — complete instead, or do it in Asana.
- Before updating a task found by search, confirm the task (name + project)
  with the user unless unambiguous.

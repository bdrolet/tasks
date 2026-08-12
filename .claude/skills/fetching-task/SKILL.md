---
name: fetching-task
version: 1.1.0
description: >
  Use when the user wants to read a specific Asana task's full content —
  description, due date, tags, comments — by task GID. Use for "show me that
  task", "what does the task say", "read the comments on that task". Use
  after searching-tasks to open a selected result.
metadata:
  depends-on: "searching-tasks"
---

# Fetching a Task

## Prerequisites

You need a task GID. If you don't have one, use **searching-tasks** first —
each result has a `task_gid`.

If the user names a task by its three-character ref (`0eh`, `gd3`) rather
than a GID, resolve it through the `refs:` map that closed the listing. Never
put a ref in the URL — the API only takes GIDs. If no listing is in context,
re-run [[searching-tasks]] to regenerate the map rather than guessing.

## Auth token

```bash
TOKEN=$(grep 'tasks_api_token' ~/src/tasks/terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
```

## Fetch

```bash
curl -s https://tasks-api.drolet.cloud/tasks/<task_gid> \
  -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Response fields:** `name`, `notes` (plain) / `html_notes`, `completed`,
`due_on`/`due_at`, `project`, `section`, `tags`, `assignee`,
`created_at`/`modified_at`, `permalink_url`, `comments`
(`[{gid, text, created_by, created_at, is_editable}]`, oldest first, system
activity excluded), `parent` (`{gid, name}` — null unless this task is a subtask), `subtasks`
(`[{task_gid, name, completed, due_on, permalink_url}]`, one level deep —
sub-subtasks are not listed), and for email-derived tasks
`message_id`/`category`/`importance`.

`404` = unknown task GID. `502` = Asana unreachable — retry once.

## Subtask refs

When `subtasks` is non-empty, give each one a ref the same way a listing does
— pipe the fetch response straight through `task-ref`, which reads the
`subtasks` array when there's no `results` array:

```bash
curl -s https://tasks-api.drolet.cloud/tasks/<task_gid> \
  -H "Authorization: Bearer $TOKEN" | task-ref
# ref  gid  due_on  name  location   (location is "—" — subtasks belong to the parent)
```

Refs are hashed from the GID, so a subtask carries the same ref here as it
does in a [[searching-tasks]] listing. Close the checklist with the same
`refs:` map, so "complete the second one" has a handle to land on.

## Presenting

- Show `notes` (plain text) rather than `html_notes`.
- If `subtasks` is non-empty, list them as a checklist, ref first, marking
  completed ones; fetch one by its `task_gid` for full detail. If `parent` is
  set, say the task is a subtask of it.
- Email-derived tasks: `notes` contains the email summary + action links; the
  `message_id` works with the fetching-inbox-email skill for the full email.
- List comments with author + date. `is_editable: true` means
  [[editing-tasks]] can edit/delete that comment.

---
name: fetching-task
version: 1.0.0
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
activity excluded), and for email-derived tasks
`message_id`/`category`/`importance`.

`404` = unknown task GID. `502` = Asana unreachable — retry once.

## Presenting

- Show `notes` (plain text) rather than `html_notes`.
- Email-derived tasks: `notes` contains the email summary + action links; the
  `message_id` works with the fetching-inbox-email skill for the full email.
- List comments with author + date. `is_editable: true` means
  [[editing-tasks]] can edit/delete that comment.

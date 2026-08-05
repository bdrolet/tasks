---
name: searching-tasks
version: 1.0.0
description: >
  Use when searching for Asana tasks — finding tasks by keyword, project,
  due date, or completion state. Use when asked to "find my task about X",
  "what tasks are due this week", "search my asana", or "list open tasks in
  project Y". Searches every project in the workspace by default.
---

# Searching Tasks

## Endpoint

```
POST https://tasks-api.drolet.cloud/search
```

Runs on the `tasks-api` Cloud Run service (tasks repo). The same service
hosts fetch and edit endpoints — see [[fetching-task]] and [[editing-tasks]].

## Auth token

```bash
TOKEN=$(grep 'tasks_api_token' ~/src/tasks/terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
```

## Request

```bash
curl -s -X POST https://tasks-api.drolet.cloud/search \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"query": "<query>", "limit": 25}' | python3 -m json.tool
```

## Parameters

| Field | Default | Meaning |
|-------|---------|---------|
| `query` | `""` | Case-insensitive substring over title + description. Empty = match all (use with filters). |
| `project` | null | Project name or GID. Null searches every project + My Tasks. |
| `completed` | `false` | `false` open only, `true` done only, `null` both. |
| `due_before` / `due_after` | null | `YYYY-MM-DD`, inclusive. Tasks without a due date are excluded when either is set. |
| `limit` | 25 | Max results (≤100). |
| `semantic` | `false` | Natural-language nearest-neighbor ranking. Use for conceptual queries ("invoices I need to pay"); keep `false` for exact keywords. |

Results are sorted due-date ascending, undated last. Each result:
`task_gid`, `name`, `project`, `section`, `due_on`, `completed`,
`permalink_url`, `snippet` (description fragment matching the query),
`parent` (parent task name — set only for subtask hits), and for
email-derived tasks `message_id`/`category`/`importance`.

Subtasks are searched too (one level deep). A subtask hit has `parent` set
and null `project`/`section` — it belongs to its parent task, not a section.

Unknown `project` returns 400 with `known_projects` — retry with one of those.

## Semantic search

Set `"semantic": true` when the query is natural language rather than a
keyword the task literally contains. Results come back best-match-first
with a `score` (cosine similarity, 0–1); `snippet` is usually null (no
literal substring). The response's top-level `semantic` flag is `false`
when the service degraded to substring ranking (embedding backend down) —
mention that if results look off. Requires a non-empty `query`.

## Presenting results

- List as: `due_on` | `name` | `project`/`section` | `permalink_url`
- Subtask hits: present as `name — subtask of <parent>`.
- Semantic hits: order is relevance, not due date — present in given order;
  surface `score` only if the user asks why something matched.
- Offer to open one with [[fetching-task]] or act on it with [[editing-tasks]].
- Email-derived tasks (`message_id` set): the inbox skills can fetch the
  underlying email.

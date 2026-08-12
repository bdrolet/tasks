---
name: searching-tasks
version: 1.1.0
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
`summary` (one-line gist of the description — lead context prose, else the
first key point; null when the task has no description),
`parent` (parent task name — set only for subtask hits), and for
email-derived tasks `message_id`/`category`/`importance`.

Subtasks are searched too (one level deep). A subtask hit has `parent` set
and null `project`/`section` — it belongs to its parent task, not a section.

Unknown `project` returns 400 with `known_projects` — retry with one of those.

## Semantic search

Set `"semantic": true` when the query is natural language rather than a
keyword the task literally contains. Results come back best-match-first
with a `score` — cosine similarity (−1..1, in practice ~0–1); `snippet` is usually null (no
literal substring). The response's top-level `semantic` flag is `false`
when the service degraded to substring ranking (embedding backend down) —
mention that if results look off. Requires a non-empty `query`.

Project-scoped semantic search (`project` + `semantic: true`) does not
return subtasks — drop the project filter to include them.

## Reference numbers

Every listed task gets a three-character base36 ref — a handle short enough
to say out loud, so the user can point at a row without reading a GID.
Never compute one yourself; pipe the response through `task-ref`:

```bash
curl -s -X POST https://tasks-api.drolet.cloud/search ... | task-ref
# ref  gid  due_on  name  location  summary   (TSV, API order preserved)
```

`task-ref` is `scripts/task_ref.py`, put on PATH by `scripts/link-skills.sh`.
`command not found` means that script hasn't been run on this machine — run
it rather than falling back to a hand-rolled numbering.

Merging several searches? Concatenate the `results` arrays into one
`{"results": [...]}` object and pipe that, so refs are assigned across the
whole set.

The ref is a hash of the GID, so the same task keeps the same ref across
listings with nothing stored. Two tasks in one listing collide about 0.6% of
the time at 25 rows; the script rehashes the loser, so a bumped task can show
a different ref in a listing its twin isn't part of. **Refs are for talking
about the list — every write path still takes the GID. Never pass a ref to an
API call.**

## Presenting results

One line per task, ref first, with the summary under it:

```
<ref> · <due_on or "—"> · [<name>](<permalink_url>) · <project>/<section>
      <summary>
```

- Group by date bucket when the request is date-shaped (**Overdue** / **Due
  today** / **Due later** / **No due date**); otherwise a flat list in the
  order the API returned.
- **Summary line** — the result's `summary`, verbatim; never write your own
  from the title, and drop the line when `summary` is null. It exists so the
  user can tell two similarly-titled tasks apart without opening either. Trim
  it to the first sentence if a listing runs long, but do not paraphrase.
- Subtask hits have `parent` set and null `project`/`section`: put
  `subtask of <parent>` in the project slot.
- Completed hits (only when `completed` was `true` or `null`): mark `✓`.
- Semantic hits: order is relevance, not due date — keep the given order;
  surface `score` only if the user asks why something matched.
- No GIDs in the list itself — they go in the map below.

Then one line: the count and the filters behind it —
`4 open tasks due on or before 2026-08-12 (all projects).`

Close with the ref → GID map, which is the handoff:

```
refs: 0eh=1217130164408154  lpb=1217286466869299  248=1217342697693471
```

- Offer to open one with [[fetching-task]] or act on it with [[editing-tasks]];
  both resolve a ref through that map before calling the API.
- Email-derived tasks (`message_id` set): the inbox skills can fetch the
  underlying email.

## When to use the task-lister agent instead

The `task-lister` agent (`subagent_type: "task-lister"`, defined in
`~/src/tasks/.claude/agents/task-lister.md`) wraps this same endpoint and
returns the same format. Prefer it when the request needs several searches
merged, a project or tag resolved first, or a natural-language request
translated into filters — it does that work without spending main-thread
context. Use this skill directly for a single straightforward search, or
whenever agent dispatch is unavailable. Either path must produce the ref-first
format above; that is the point of keeping them in sync.

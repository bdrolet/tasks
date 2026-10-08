---
name: prioritizing-tasks
version: 1.0.0
description: >
  Use when the user asks what to work on — "what should I do next", "what's my
  day look like", "why is X ranked there", "bump X to the top", "snooze that
  till Friday", "that's a 3-pointer", "I started X", "block X on Y". Reads the
  prioritizer's ranking from tasks-api and applies pins, snoozes, points,
  started-at and dependencies.
  For creating, editing, completing or commenting on tasks use the other
  task skills.
---

# Prioritizing Tasks

## Endpoints (tasks-api, Cloud Run IAM)

```bash
TOKEN=$(gcloud auth print-identity-token)
BASE=https://tasks-api.drolet.cloud
curl -s -XPOST "$BASE/next" -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{}'
curl -s "$BASE/ranking?bucket=next&limit=100"          -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/ranking?list=overcommitted"             -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/ranking?explain=true"                   -H "Authorization: Bearer $TOKEN"
curl -s "$BASE/calibrate"                              -H "Authorization: Bearer $TOKEN"
curl -s -XPUT "$BASE/tasks/<gid>/overrides" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"pinned_rank": 1}'
curl -s -XPATCH "$BASE/tasks/<gid>" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"story_points": 3, "started_at": "2026-09-23"}'
curl -s -XPATCH "$BASE/tasks/<gid>" -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -d '{"add_dependencies": ["<blocker gid>"]}'   # or remove_dependencies
```

Or the CLI, which does the same and prints ref-first TSV: `task-next`,
`task-next ranking`, `task-next start|points|pin|unpin|snooze|unsnooze|override <ref|gid> …`,
`task-next block|unblock <ref|gid> <blocker ref|gid>`, `task-next calibrate`.

## Meaning

- `POST /next` — today's selection (capacity 5 points, diversity across
  projects, optional `energy` deep|shallow and `n`) plus `overcommitted`,
  `stale`, `nudge`. Logs a `manual` run; never bumps deferral counters.
  - Must-dos first: a hard `due_on` whose effective slack (days until due
    minus the work it needs minus hard work queued ahead) is ≤ 5 leads the
    list whatever its score, beyond `n` and capacity, and consumes capacity.
  - Inbox is excluded: its tasks bucket `excluded:project`, never selected;
    a pin does not override that.
  - Starvation boost: a project not in a recent daily pick gets up to +50%
    at selection (`starvation_boost`), so no board goes days unpicked.
  - Nudge is presented grouped by `waiting_on` (who is owed), largest first.
  - Subtasks inherit: snoozing or blocking a parent, or a wait Ben set on
    it (`waiting:` tag or override), covers its subtasks
    (`components.inherited` names the ancestor) unless the subtask has its
    own `waiting:` tag or override (including `""`, not waiting); a subtask's
    model guess never overrides a parent's hand-set wait, and a
    model-inferred wait on a parent does not inherit. A pin overrides an
    inherited block or wait, never an inherited snooze.
  - A model-inferred wait counts only at medium/high `waiting_confidence`,
    and is set aside on a hard-dated task once slack ≤ 5 days
    (`wait_released: true`, shown as `waiting? <who>`).
  - An undated subtask takes its nearest ancestor's due date as a hard date
    (`components.due_from` names the ancestor), so it can be a must-do.
- Dependencies ("block X on Y" / "X depends on Y" / "unblock X from Y"):
  `PATCH /tasks/{gid}` `add_dependencies` / `remove_dependencies` (GIDs). This
  skill never creates a task — if Y does not exist, say so and hand creation
  to `task-builder`.
- `GET /ranking` — every task in score order. `bucket` = `next` (default) |
  `nudge` | `snoozed` | `excluded`; or `list` = `overcommitted` | `stale` |
  `nudge`. `explain=true` adds `components` (P, U, I, B, A, C, points,
  effective_due, due_source (hard | inferred | horizon | none), soft, slack,
  effective_slack, days_stale, starvation_boost, waiting_source (tag | override | model | none), waiting_confidence, wait_released, unenriched) and
  the model's one-line `reason`.
- Overrides (`PUT /tasks/{gid}/overrides`, null clears; `waiting_on: ""` means not waiting — CLI `waiting_on=-`): `pinned_rank`
  (holds that position regardless of score), `snooze_until`, and field
  overrides `waiting_on`, `impact`, `energy`, `due_date_inferred`,
  `story_points`. Tags beat overrides: `impact:high`, `energy:deep`,
  `waiting:<who>`.
- The ranking is materialised by events; a write is reflected within
  seconds, not instantly — re-read after a write before reporting the new order.

## Refs

Pipe `{"results": <tasks array>}` through `task-ref` to label rows, exactly as
`searching-tasks` does. Every write takes the GID.

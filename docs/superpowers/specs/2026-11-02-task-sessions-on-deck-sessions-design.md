# task-sessions on deck-sessions

**Date:** 2026-11-02
**Status:** implemented (PR #57)
**Umbrella spec:** `~/src/scripts/docs/superpowers/specs/2026-11-02-deck-sessions-design.md`
— read it first. It defines `deck-sessions`, its JSON contract, the verified
agent-deck behaviour, and the rollout order (this is step 2; `deck-sessions`
must be built and on PATH first).

**Predecessor:** `2026-11-02-agent-deck-task-sessions-design.md` (this repo,
PR #55) — the current agent-deck implementation and its experiments.

## Problem

`scripts/task_sessions.py` (PR #55) contains two halves:

- **Task half** — tasks-api search/fetch, `context_block`, refs, `START_PROMPT`,
  title, CLI flags, TSV/JSON output.
- **agent-deck half** — `listing`, `hints`, `find_sessions`, `act` (the state
  machine), `launch_argv`, `ensure_group`, `check_parent`, `resolve_placement`
  plumbing, `deck_section`, `write_context`, `_refresh`, `prune`.

The agent-deck half now moves to `deck-sessions` so `outreach-sessions` in
`~/src/people` can share it. This change is a refactor: **no behaviour change**.

## Design

### D1. What stays in `task_sessions.py`

- tasks-api: `token`, `_call`, `search`, `fetch`.
- `context_block(task, ref)`, `session_name(ref, name)`, `START_PROMPT`,
  `ref_for` (unsalted `task_ref.ref`).
- Config resolution of group and parent (flag → env → default:
  `TASK_SESSIONS_GROUP`/`tasks`, `TASK_SESSIONS_PARENT`/`conductor-tasks`),
  including the `--idle` / `--prompt` / `--prompt-file` rules and the
  `--inherit-group` validation. These are task policy; they produce spec
  fields.
- CLI flags and both output formats, byte-for-byte as today (TSV row
  `ref gid due project/section name permalink → outcome`; `--json` rows with
  `ref, gid, name, due, project, section, permalink, outcome, deck_id, status,
  group, attach`). Map the `deck-sessions` result's `outcome` + `deck_id`
  back to today's strings (`launched <id8>`, `already open <id8> (<status>)`,
  `reopened <id8>`, `refreshed <id8>`, `duplicate <id8>,<id8>`,
  `failed: <detail>`, `would launch`, ...).
- `skipped (completed)` and `fetch failed: ...` rows (never sent to
  `deck-sessions`).

### D2. What goes

Everything listed as the agent-deck half above. `task_sessions.py` builds one
spec per task and pipes JSONL to `deck-sessions ensure [--apply]`:

```json
{"key_hint": "asana_gid", "key": "<gid>", "title": "<ref> <name≤60>",
 "adopt_prefix": "<ref> ", "context": "<context_block>",
 "context_path": "~/.claude/task-context/<gid>.md",
 "cwd": "/Users/ben/src/tasks", "group": "tasks", "parent": "conductor-tasks",
 "parent_is_default": true, "hints": {"permalink": "<url>"},
 "model": null, "idle_timeout": null,
 "prompt": "<START_PROMPT unless --idle>", "reopen_prompt": null,
 "refresh": false}
```

`context_path` keeps today's location: the 13 live task sessions were
launched with `--append-system-prompt-file ~/.claude/task-context/<gid>.md`
baked into their stored extra args, so that directory must keep existing.

`--prune`: `deck-sessions sessions --key-hint asana_gid --group <group>` →
fetch each GID from tasks-api → completed or 404 → `deck-sessions archive
[--apply] <ids>`. Output lines unchanged (`pruned <id8>`, `kept (busy)`,
`would prune`).

Exit codes unchanged: 2 when `deck-sessions` reports the registry
unavailable; also 2 (with `deck-sessions not on PATH`) when the binary is
missing.

### D3. Tests

`tests/test_task_sessions.py` keeps the context, title and config tests, and
replaces the `FakeDeck` agent-deck fake with a fake `deck-sessions` runner
(records the JSONL it was given, returns canned results). Assert:

- one spec per task with the fields above; `--idle` → `prompt: null`;
  `--prompt` overrides; `--parent none` → `"parent": "none"`;
  `--group`/env precedence; `--refresh` sets `refresh: true`;
- `--apply` passed only with `--spawn`;
- outcome mapping to today's TSV/JSON strings for every `deck-sessions`
  outcome;
- prune: `sessions` → fetch → `archive` with only the completed task's id;
- registry-unavailable and missing-binary exit 2.

The agent-deck behaviour tests move to `~/src/scripts/deck-sessions/tests/`
(umbrella D4); delete them here once they pass there.

### D4. Docs

- `CLAUDE.md` "Per-task sessions": say the agent-deck mechanics live in
  `deck-sessions` (umbrella spec path); keep the task-specific rules (group,
  parent, start prompt, prune semantics). `link-skills.sh` warns when
  `deck-sessions` is not on PATH.
- `.claude/agents/task-launcher.md`: no change expected (same CLI and output).
- Update the predecessor spec's status line to point here.

### D5. Sublists

Sessions the conductor moved into `tasks/waiting-on-others`,
`tasks/waiting-on-ben` or `tasks/done` must still be found and pruned. Keep
the `in_tree` tests passing through `deck-sessions` (umbrella fact 15). Done:
`~/src/scripts/deck-sessions/tests` covers lookup and `sessions --group` for
sublists; `--prune` here passes `--group <group>` to it.

## Acceptance

1. `.venv/bin/pytest tests/ -q` passes.
2. Live, with the 13 sessions still in group `tasks` under `conductor-tasks`:
   `task-sessions <those 13 gids> --spawn` reports every one `already open`
   and launches nothing; `agent-deck list --json` shows no new sessions.
3. One new task: dry run shows `would launch`; `--spawn` launches it into
   `tasks` under `conductor-tasks` with the start prompt; a rerun reports
   `already open`.
4. `task-sessions --prune` (dry run) lists sessions of completed tasks, if any.

Open a PR per the repo workflow (`/pr-open`); wait for CI before merging.

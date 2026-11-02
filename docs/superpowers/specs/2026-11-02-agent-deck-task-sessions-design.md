# Task sessions on agent-deck

**Date:** 2026-11-02
**Status:** implemented on branch `agent-deck-task-sessions`; assumptions verified live against agent-deck v1.16.36 (see "Experiments")

## Problem

`scripts/task_sessions.py` (`task-sessions` on PATH, driven by the
`task-launcher` agent) gives each Asana task a background Claude Code session
via `claude --bg --append-system-prompt <context>`, tracks GID → short session
id in `~/.claude/task-sessions.json`, and reconciles that map against
`claude agents --json --all`. Ben now runs his sessions in agent-deck, and the
current launcher has defects of its own:

1. A failed `claude agents` call returns an empty set, `reconcile` drops every
   entry, and every task is spawned again (duplicates).
2. `--all` lists exited sessions, so a stopped session blocks its task until
   someone runs `claude rm`; there is no resume path.
3. The session id is parsed out of human-readable `backgrounded · <id> · …`
   output.
4. Only the 8-character id is stored, matched exactly.
5. A GID given twice is spawned twice.
6. Explicit GIDs are silently capped by `--limit`.
7. State is saved once at the end, outside `finally`; an exception mid-run
   loses the record of sessions already spawned.
8. Sessions made by hand are invisible to the launcher.
9. No choice of model, prompt, group or parent.
10. The agent's report wants permalink/project/section, which the script never
    prints.

## Goals

- Every task session is an agent-deck session, visible in Ben's normal
  agent-deck TUI and filterable by group.
- A session **starts working when it is created**: the launch sends a start
  prompt by default. `--idle` opts out (warm session at an empty prompt, no
  tokens until someone types).
- **Group and parent are configurable**, with safe defaults (`tasks`, no
  parent).
- agent-deck's registry is the only state. No GID map file.
- A stopped session is reopened with its conversation intact, never duplicated.
- Fix defects 1–10.

## Non-goals

- A launcher shared with `~/src/people`'s `outreach-sessions`. The logic here
  is written so it can be lifted into a shared `deck-sessions` CLI later, but
  that move happens when outreach migrates, not now.
- The `task-launcher` agent driving agent-deck directly. Haiku following a
  multi-step recipe is less reliable than one deterministic script call, and
  it would fork the agent path from the direct path, which CLAUDE.md requires
  to produce the same output.
- Attaching from inside Claude. `agent-deck session attach` is a TUI and
  needs a real terminal, same as `claude attach` today.

## Design

### D1. Backend: `agent-deck launch`, serially

One `launch` per task, one at a time (agent-deck's gotchas warn that parallel
launches thrash). `launch` registers and starts the session in one step;
without a message, Claude sits at an empty prompt and spends nothing.

```
agent-deck launch --json --trust-folder -c claude --no-identity \
  <parent flags (D3)> <group flags (D2)> \
  -t "<ref0> <name≤60>" \
  --hint asana_gid=<gid> --hint permalink=<url> \
  [--model <m>] [--idle-timeout <dur>] \
  --extra-arg=--append-system-prompt-file --extra-arg=<context file> \
  [--message-file - --no-assert-done]   # only with --prompt (D6)
  /Users/ben/src/tasks
```

`launch --json` returns `id`, `claude_session_id`, `tmux_session`, `group`,
`hints` and `status`, so no human-readable output is ever parsed (defect 3).
A failure returns `success: false` with `error` and `code`.

`--trust-folder` avoids the `FOLDER_NOT_TRUSTED` warning agent-deck emits for
the repo. `launch` locks the title, so Claude cannot rename the session.

### D2. Group: configurable, default `tasks`

| Source | Precedence |
|---|---|
| `--group NAME` | 1 |
| `TASK_SESSIONS_GROUP` | 2 |
| built-in | `tasks` |

The value is passed as `-g <group>`. `--inherit-group` drops `-g` and passes
`--inherit-group`, placing the session in the parent's group; it is an error
without a parent, or together with an explicit `--group`. Without `-g`,
agent-deck would derive the group from the folder, so `-g` is always sent
otherwise.

Before the first launch of a run, `agent-deck group show <group> --json`:
if the group does not exist, `group create <group> --default-path
/Users/ben/src/tasks` then `group update <group> --max-concurrent 0` (create
ignores a 0 cap); if it exists with
`max_concurrent != 0`, print a warning (a cap of 1 would serialize sessions
given a prompt). The docs say auto-created groups default to 1; the live
install auto-created one with 0, so the script checks rather than trusts
either.

### D3. Parent: configurable, default the tasks conductor

| `--parent` / `TASK_SESSIONS_PARENT` | Flags sent |
|---|---|
| unset (default) | `--parent conductor-tasks`; `--no-parent` + warning if that session is not in the registry |
| `none` | `--no-parent` |
| `self` | `--parent $AGENTDECK_INSTANCE_ID`; error if unset |
| `<id or title>` | `--parent <value>` |

`--no-parent` is mandatory by default: verified live, a `launch` without it
from inside an agent-deck session auto-parents under the calling session (the
launcher agent runs inside Ben's session, so every task session would become
its child). `self` resolves the id explicitly rather than relying on that
auto-parenting.

A parent receives the child's turn transitions and its
`===AGENTDECK_DONE===` line in its inbox. That is only useful when the child
works on its own (D6 `--prompt`); for hand-worked sessions it is noise, which
is why the default was first none. Revised 2026-11-02: the default is the
dedicated agent-deck conductor `conductor-tasks`, so every launcher-made
session is supervised and nothing else is. The conductor's `POLICY.md` limits
it to children carrying an `asana_gid` hint. An explicit parent that is not
in the registry is an error; the default degrades to no parent with a warning.
Sessions launched before this change were moved with
`agent-deck session set-parent <id> conductor-tasks` and sent the sentinel
rule as a message, since their context file predates it.

### D4. Context delivery: one file, our identity section

`context_block(task)` (unchanged content) is written to
`~/.claude/task-context/<gid>.md`, mode 0600, and passed as
`--append-system-prompt-file` via `--extra-arg`.

**Verified constraint:** agent-deck already passes its own
`--append-system-prompt-file <identity.md>`, and Claude keeps only the
**last** file flag — the probe session saw our context and not agent-deck's
identity block. Inline `--append-system-prompt` is not a way around this:
a 36 KB context failed with tmux `command too long`, and inline text is
stored in plaintext in state.db. There is no config hook to append content to
agent-deck's identity file.

So the launcher passes `--no-identity` (making the drop explicit) and the
context file carries its own short `## agent-deck` section:

- always: "You run in agent-deck as session `<title>` in group `<group>`;
  `agent-deck session current --json` returns your live record."
- with a parent: the completion rule — end the final message of finished work
  with one line `===AGENTDECK_DONE=== status=<ok|fail> summary=<one line>` —
  and that a line starting `NEED:` asks the parent a question.

The context file is a cache, rewritten on every run; it is not state.

### D5. Identity and lookup: registry only, no state file

Identity is the hint `asana_gid=<gid>`. The title is `"<ref0> <name>"` where
`ref0 = task_ref.ref(gid)` (unsalted, so it does not depend on what else is
in the listing); it survives an Asana rename because the ref is a hash of the
GID.

Lookup per run:

1. `agent-deck list --json`. Non-zero exit or not a JSON array → every row is
   `registry unavailable`, nothing is launched, exit 2 under `--spawn`
   (defect 1).
2. Candidates: rows in the configured group, plus rows in any group whose
   title starts with a target's `"<ref0> "`.
3. For each candidate, `agent-deck session annotate <id> --json` → read
   `hints.asana_gid`. (`list --json` and `session show --json` do not include
   hints.)
4. Candidate with a matching ref prefix and no hint = made by hand: adopt it
   (`session annotate <id> --set-hint asana_gid=<gid>`) under `--spawn`
   (defect 8).
5. Per target: zero rows → launch; one → D7; two or more →
   `duplicate <id8>,<id8>`, no action.

agent-deck independently refuses a second session with the same title at the
same path (`code: ALREADY_EXISTS`, verified), which the script reports as
`already open` after re-listing — a second guard, not the primary one.

GIDs are de-duplicated preserving order (defect 5). `--limit` applies to
`/search` only; explicit GIDs are never capped (defect 6). The existing
10-per-dispatch cap stays in the agent.

### D6. Start prompt by default

Every launch delivers a first message via `launch --message-file -`, so the
session starts working immediately — the outreach-sessions "prep" pattern.
The default is `START_PROMPT` in the script: re-fetch the task, work out what
it requires, research and do safe, reversible prep, then report what it
requires, what was found and done, the next step and what Ben must supply. It
forbids outward-facing or irreversible actions (email, payments, forms,
editing or completing the task) without asking, and asks the session to flag
an apparent duplicate task and stop. `--prompt TEXT` / `--prompt-file F|-`
replaces it; `--idle` sends nothing. Reopened sessions are never prompted.
`launch` adds a done-sentinel instruction to a message by default; the script
passes `--no-assert-done` unless a parent is set (D4 already carries the rule).

Decided 2026-11-02 after the first live run: idle-by-default sessions were
created but did nothing until someone typed, which was not what "start when
created" meant.

### D7. State machine

| Found | Action | Outcome |
|---|---|---|
| none | launch | `launched <id8>` |
| `running` / `waiting` / `idle` | nothing | `already open <id8> (<status>)` |
| `stopped` / `error` | `session start <id>` | `reopened <id8>` |
| archived | `session unarchive`, then start | `reopened <id8>` |
| `--refresh` and `waiting`/`idle` | send refresh (D8) | `refreshed <id8>` |

Verified: `session stop` then `session start` resumes with
`claude --resume <claude_session_id>`; the conversation and the
`claude_session_id` are kept. A dry run (default) prints `would launch`,
`would reopen`, `would refresh`. Failures print `failed: <first line of
error>`. Rows are processed and recorded one at a time inside `try`, so a
failure on one task never loses the others (defect 7).

### D8. Refresh sends a message; rewriting the file does nothing

**Verified:** after the context file was rewritten, neither `session start`
nor `session restart --force` exposed the new content — a resumed Claude
conversation keeps its original system prompt. So `--refresh` delivers the
freshly fetched context block as a message:

```
agent-deck session send --defer-if-busy --no-tag --message-file - <id>
```

prefixed "Updated task context (Asana snapshot as of <time>); supersedes the
one in your system prompt:". This costs one turn per refreshed session, so it
is opt-in and never automatic. Note `session send` has no `--message` flag;
the text is positional or `--message-file`.

### D9. Prune

`--prune` fetches `/tasks/{gid}` for every session in the group carrying an
`asana_gid` hint. Completed or 404, and status not `running` → `session stop`,
`session archive`, outcome `pruned <id8>`; a running session is
`kept (busy) <id8>`. Archive keeps the transcript and a later run reopens it.
Explicit, never automatic: the Asana webhook cannot reach the laptop, but
silently stopping sessions on a run that was asked to launch would surprise.

### D10. Output

Tab-separated, one row per task:
`ref  gid  due  project/section  name  permalink  → outcome`, plus `--json`
emitting the same rows as objects with `deck_id`, `status`, `group` and
`attach`. Closing hint on stderr:

```
agent-deck session attach <id8>     # needs a real terminal; Ctrl+Q detaches
agent-deck -g <group>               # TUI scoped to task sessions
```

### D11. Agent, docs, install

- `task-launcher.md`: call `task-sessions <gids> --spawn --json` (plus
  `--group`/`--parent`/`--prompt` only when the request names them); report
  `due · project/section · agent-deck session attach <id8>` from the JSON
  rows instead of looking those fields up; explain `registry unavailable`
  and `duplicate` as stops. Never run `agent-deck` itself.
- CLAUDE.md "Per-task sessions": rewrite for agent-deck — group/parent
  settings and precedence, the hint as identity, the state table, refresh
  and prune, attach command, "no state file".
- `scripts/link-skills.sh`: warn if `agent-deck` is not on PATH.

### D12. Migration

`~/.claude/task-sessions.json` has 9 entries; 4 are still listed by
`claude agents --json --all`, all in state `done`. They are not migrated:
the file is deleted, and those sessions are left for `claude rm`. A task
whose old session matters gets a fresh agent-deck session on its next launch.

## Testing

`tests/test_task_sessions.py` keeps the `context_block`/`session_name` tests
and replaces the `claude --bg` ones with a `FakeDeck` runner that holds rows
and hints and answers `list`, `launch`, `session show|start|stop|archive|
unarchive|annotate|send` and `group show|create`, recording every argv:

- launch argv: `--no-parent` by default, `--no-identity`, `-g tasks`, the
  `--extra-arg=` pair, no `--message-file` without `--prompt`
- group precedence (flag > env > default) and `--inherit-group` errors
- parent modes, including `self` without `AGENTDECK_INSTANCE_ID`
- context file 0600 and the parent-only sentinel section
- every D7 row; `registry unavailable` launches nothing; duplicate refusal;
  hand-made adoption; renamed task still found; `ALREADY_EXISTS` handling
- GID dedupe and the `--limit` rule; a failure mid-run keeps earlier rows
- `--refresh` skips running sessions; `--prune` by status

`main()` gains injectable `runner`, `fetch` and `search` seams so it is
tested end to end. A manual check after merge: launch one real task, attach,
stop, rerun (expect `reopened`), `--refresh`, `--prune` after completing it.

## Experiments (2026-11-02, agent-deck v1.16.36, throwaway profile)

| Question | Result |
|---|---|
| Does `launch --json` return the ids? | Yes: `id`, `claude_session_id`, `tmux_session`, `hints`, `status`; failures carry `error` + `code` |
| Two `--append-system-prompt-file` flags | Both on the command line; Claude used only the last (ours). Identity block lost |
| Inline `--append-system-prompt`, 36 KB | Fails: tmux `command too long` |
| Duplicate title, same path | Refused, `ALREADY_EXISTS` with the existing id |
| Group concurrency | Auto-created group had `max_concurrent: 0`; two sessions ran side by side |
| Stop then start | Same `claude_session_id`, `--resume`, conversation kept |
| Rewritten context file after start / restart | Not picked up in either case |
| Launch without `--no-parent` inside a session | Tried to auto-parent under the caller |
| `--parent X -g G` | Child in group G, parent recorded |
| `session send --message` | No such flag; positional or `--message-file` |

| `group create --max-concurrent 0` | Ignored: group stored with 1. `group update --max-concurrent 0` works, so the script uncaps a group it just created |
| `session annotate <id> --json` | `{hints: {...}, tags: [...], links: [...], success}` |
| 13 real tasks, launched serially with the start prompt | All launched (~2 min); one finished its first report within the run |

Not verified: hints surviving archive/unarchive, `--idle-timeout`
persistence (not shown in `session show --json`).

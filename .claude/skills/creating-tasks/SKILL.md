---
name: creating-tasks
version: 1.0.0
description: >
  Use when the user wants a new Asana task created from a rough request — "add a
  task", "make a task to X", "create a task from that email", "remind me to Y",
  "put that on my list". Dispatches the task-builder agent, which researches and
  fills the task out. For updating, completing, or commenting on a task that
  already exists, use editing-tasks.
metadata:
  depends-on: "task-builder (agent), editing-tasks, searching-tasks"
---

# Creating Tasks

New tasks go through the **task-builder** agent, never a bare `POST /tasks`. The
agent researches related email, checks for duplicates, and fills the task out to
the content standard. Your job is the handoff and the one question it can't ask.

## 1. Dispatch

Spawn the `task-builder` agent (`subagent_type: "task-builder"`) with the raw
request **plus the context the session already has** — the agent starts blank and
cannot see this conversation. Pass along anything relevant:

- the request in the user's own words, not your paraphrase
- a message ID / email under discussion, and its mailbox label
- a person, project, file, or PR named earlier in the session
- the parent task GID when the request is a subtask ("add a subtask under
  X") — find X with searching-tasks first if the GID isn't at hand
- any deadline or priority the user stated explicitly
- today's date

Missing context is the main failure mode: the agent re-derives it badly or misses
it entirely. Thin dispatches produce thin tasks.

Do not research first and hand over findings — that's the agent's job, and doing
it twice wastes a round-trip.

## 2. Relay

The agent returns `CREATED`, `DUPLICATE`, or a failure.

**`CREATED`** — report the permalink, the title with its priority, and the due
date. Surface open questions if there are any; otherwise stay brief.

**`DUPLICATE`** — the agent stopped without creating. Show the user both the
existing task and the drafted one, and ask which they want:

> Found an existing task for this: `<name>` (<permalink>). Create the new one
> anyway, update that one instead, or drop it?

- *create anyway* → re-dispatch with `skip_dedup: true` in the prompt
- *update instead* → use **editing-tasks** on the existing task
- *drop* → done

**Failure** — relay the error and the state it reported. If a task was created
but comments failed, say so; the task exists and needs no retry from scratch.

## 3. Corrections

Fixes after the fact go through **editing-tasks** (`PATCH /tasks/<gid>`), not a
second agent run. Re-dispatch only when the request itself was wrong.

## When not to use this

- **A batch of tasks from a brief** → **planning-project-tasks**, which decides
  the container and creates the whole tree under one approval.
- **Changing an existing task** → **editing-tasks**.
- **The user dictated the exact task and wants it verbatim** — e.g. handing you a
  precise title, or adding to a running list — the agent's research is overhead.
  Create it directly with **editing-tasks**.

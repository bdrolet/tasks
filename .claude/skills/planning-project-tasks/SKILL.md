---
name: planning-project-tasks
version: 1.0.0
description: >
  Use when the user hands over a project brief or a detailed list of things to
  be done and wants them created as Asana tasks — "plan this project", "turn
  this into tasks", "ingest these into Asana", "break this down into tasks". For
  creating or editing a single task, use editing-tasks instead.
metadata:
  depends-on: "searching-tasks, editing-tasks"
---

# Planning Project Tasks

Turn a prose brief into a reviewed set of Asana tasks. You decide the structure
from what already exists; you always show the plan before writing anything.

## 1. Understand the brief

Draft the candidate task breakdown. Ask clarifying questions ONLY where it is
genuinely ambiguous: scope boundaries, hard deadlines, or priority. Don't over-ask.

## 2. Inspect existing state

Dispatch the **inspecting-asana-state** subagent (`agents/inspecting-asana-state.md`)
with your candidate task names. It returns a digest: existing projects + their
sections, the tag vocabulary, and per-candidate duplicate matches (the same task
search the **searching-tasks** skill performs). (Fallback if dispatch fails: run
the curls in that agent file inline.)

## 3. Decide the container (your call)

Pick ONE from the digest:

- **New project** — a big or distinct initiative with no good existing home →
  create a project with sections.
- **Nest in an existing project** — the brief clearly belongs to one that exists
  → tasks into its EXISTING sections (this skill can't add sections to an
  existing project).
- **Parent + subtasks** — a small list → one parent task in the default tasks
  project, items as subtasks.

Reuse existing sections and tags rather than inventing near-duplicates.

## 4. Propose the plan, get ONE approval

Show the user before any write: the container choice (names), the full task tree
(names, priorities, due dates, key points) with subtasks nested, and **dedup
flags** — for each likely duplicate, name the existing task and ask whether to
skip. Never auto-skip. Wait for explicit approval; revise if asked.

## 5. Create (orchestrate, in order)

Create tasks with the **editing-tasks** skill's `POST /tasks` (same fields). Order:

1. If a new project is warranted → `POST /projects` with
   `{"name":..., "sections":[...]}`; keep the returned `sections` name→gid map.
2. Each top-level task → `POST /tasks` into its section (omit `parent`).
3. Each subtask → `POST /tasks` with `parent` = the parent's `task_gid` and NO
   `project`/`section` (a subtask belongs to its parent, never a section).

Report each created `permalink_url`. There is no rollback — if a call fails
partway, list what was created vs. failed and offer to retry the remainder.

# Task content standard

Every task in the configured Asana project follows this shape so a reader
always knows where to look. Structured attributes live in native Asana slots;
the description holds prose in a fixed order.

## Where each thing goes

| Information | Slot |
|---|---|
| Priority (P0–P3) | Title prefix `[P1] …` — see **Title** below |
| Category (urgent/respond/review) | Section |
| Topic | Tags (kebab-case: `finance`, `passport-renewal`) |
| Due date | `due_on` (or `due_at` only when time-of-day matters) |
| Assignee | Native assignee |
| Origin / provenance | Description **footer** (`Source: …`) |
| Substance (summary, links, next action) | Description template |

## Title

`[PX] {verb} {object}` — an actionable next step, not the raw email subject.
**This section is authoritative:** any code that generates, prefixes, normalizes,
or falls back on a title must conform; if code and this section ever disagree,
**this section wins** (fix the code).

- `[PX]` — the priority prefix (`P0`–`P3`), added in code, never by the model
  (automatic on email tasks; optional on manual/API tasks).
- `{verb}` — an imperative, present-tense verb chosen from the email's actual
  content (context-driven, not a fixed category→verb map). Carries the "what do
  I do" signal.
- `{object}` — 2–5 words naming what the action is on, specific enough to tell
  the task apart from its siblings. Drop `Re:`/`Fwd:`, list tags, and pleasantries.

Sentence case, no trailing punctuation, ≤ ~60 chars after the prefix so the
title never truncates in the list view. Best-effort: if enrichment yields no
usable title, fall back to `[PX] {subject}`; a missing title never blocks task
creation.

Implemented in: `services/email_summary.py` (generates `{verb} {object}`),
`handlers/task_create.py` (prepends `[PX]`), `clients/asana.py::create_task`
(fallback), `api/routers/tasks.py::_title` (manual/API path).

Examples: `[P0] Review Q3 board deck`, `[P1] Reply to Sarah on contract
redlines`, `[P0] Resolve prod DB disk alert`.

## Description template (fixed order)

`context → key points → links → actions → source footer`. Sections omit when
empty; the source footer is always present (`Created manually` for manual tasks).

A PATCH that sets any content field (`context`, `key_points`, `links`,
`action_items`) rewrites the entire description from those fields, including
the source footer — patching an email-originated task's content via the API
resets its footer to `Source: Created manually`.

## Conventions

- **Priority** — the `[P0–P3]` title prefix, nothing else (see **Title**). Don't
  put priority in a tag or a description line.
- **Tags** — kebab-case topic words only. Not status, not priority (those have
  their own slots).
- **Due dates** — set only when a real date is known; never fabricate one.
- **Comments** — terse, single-purpose status updates ("waiting on Alice"), not
  a restatement of the description.

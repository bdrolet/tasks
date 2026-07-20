# Task Content Standard — Design

**Date:** 2026-07-20
**Status:** Approved design, ready for implementation plan

## Problem

Tasks land in the Asana email-tasks project from two independent paths with no
shared notion of how a task should be shaped:

1. **Automated pipeline** — `handlers/task_create.py` → `clients/asana.py::create_task`.
   Title is hardcoded `[{importance}] {subject}`; the HTML description is built
   by a bespoke `clients/asana.py::_html_notes()` (metadata header, AI reasoning,
   key points, links, confirm/review/reference/ignore action links, draft-reply
   link).
2. **Manual / agent path** — `tasks-api` (`api/routers/tasks.py`) plus the
   `editing-tasks` Claude Code skill. Fully free-form: title, `description` /
   `html_description`, tags, due date, assignee are whatever the caller passes.

There is no declared standard for what belongs on a task, so manually-created
tasks are inconsistent with pipeline tasks and with each other. We want one
standard for title and description structure (plus documented conventions for
priority, tags, due dates, and comments), enforced in code where it matters.

Two structural problems get fixed along the way:

- `_html_notes()` is business logic (content shaping) living in `clients/`,
  which the repo's layer rules reserve for I/O only. It moves to `services/`.
- The manual API accepts an opaque description blob, so the structure is
  unenforceable there. It becomes a structured schema the API assembles.

## Goals

- One shared content model and one HTML renderer, used by **both** creation paths.
- Every task states its origin (a mandatory source line).
- Manual/agent task creation goes through structured fields, not a free-form blob.
- Written conventions for priority, tags, due dates, and comments, referenced
  from the `editing-tasks` skill so the agent path follows them.

## Non-goals

- No change to task **search**, fetch, completion, escalation, or webhook flows.
- No priority field on manually-created tasks (priority stays an
  email-classification concept — see Conventions).
- No forced tag vocabulary or tag validation in code (convention only).
- No comment schema change (convention only).
- No database schema change.

## Design

### 1. Shared content model — `models/task_content.py`

A pure dataclass, no imports from other layers (per layer rules):

```python
@dataclass
class TaskContent:
    metadata: list[tuple[str, str]] = field(default_factory=list)     # (label, value) header rows
    context: str | None = None                                        # freeform paragraph (AI reasoning today)
    key_points: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)        # (url, label)
    action_items: list[tuple[str, str]] = field(default_factory=list) # (label, url)
```

Field meanings:

- **metadata** — the header block: labelled rows rendered as a bulleted list.
  The automated pipeline fills From / To / Cc / Received / Importance / Tags.
  Manual callers may pass their own rows or none.
- **context** — a freeform paragraph. Holds the automated pipeline's "AI
  reasoning"; for manual tasks it's the general "what is this / why" text.
- **key_points** — bullet list of specifics (actions requested, facts).
- **links** — labelled hyperlinks.
- **action_items** — labelled hyperlinks rendered under an "Actions" heading;
  used by the pipeline for the confirm/review/reference/ignore label links.

### 2. HTML renderer — `services/task_content.py`

```python
def render_html_notes(content: TaskContent) -> str: ...
```

- Replaces `clients/asana.py::_html_notes()`. HTML escaping (the existing
  `_esc` behavior — `&`, `<`, `>`) moves here.
- Section order: **metadata → context → key_points → links → action_items**,
  each section omitted when its field is empty (except the mandatory source
  line below).
- Wraps output in the `<body>…</body>` root Asana requires (same requirement
  `api/routers/tasks.py::wrap_html_body` handles today).
- **Mandatory source line:** if `content.metadata` is empty, the renderer
  prepends a single metadata row `Source: Created manually`. The automated
  pipeline always supplies explicit metadata, so this default only appears on
  manual tasks that pass none. This guarantees every task states where it came
  from.

The renderer is pure (TaskContent in, HTML string out) — no I/O, unit-testable
in isolation.

### 3. Automated pipeline changes

- `clients/asana.py::create_task` stops calling `_html_notes()`. Instead,
  `handlers/task_create.py` builds a `TaskContent` from the event + enrichment
  (the metadata rows, reasoning as `context`, key points, links, and the
  category-dependent action links currently assembled in `_html_notes`) and
  passes the rendered `html_notes` string down. The action-link URL construction
  (`action_url`, webhook token) moves to the handler/service layer alongside the
  rest of the content shaping — it is not I/O and does not belong in the client.
- Title unchanged: `[{importance}] {subject or '(no subject)'}`.
- Net effect: `clients/asana.py` shrinks to I/O only; `_html_notes`, `_esc`,
  and the action-URL helper leave the client.

### 4. Manual API changes — `api/routers/tasks.py`

**Breaking change to the request bodies** (blast radius is fully contained —
consumers are the `editing-tasks` skill doc and this repo's tests only):

`CreateTaskRequest` and `UpdateTaskRequest` drop `description` and
`html_description`. They gain structured content fields mirroring `TaskContent`:

```python
metadata: list[tuple[str, str]] = []   # optional header rows
context: str | None = None
key_points: list[str] = []
links: list[tuple[str, str]] = []
action_items: list[tuple[str, str]] = []
```

- The router builds a `TaskContent` from these and calls
  `render_html_notes` to produce `html_notes`.
- On **create**, content fields are optional; if all are empty the task gets
  just the mandatory `Source: Created manually` line.
- On **update (PATCH)**, presence of any content field means "rewrite the
  description from these fields." Content fields absent → description untouched
  (same set-based semantics the PATCH handler already uses for other fields).
  Partial-field merge of an existing description is **out of scope** — a
  description update replaces, it does not merge.
- Title, tags, due dates, assignee, section, completion: **unchanged**.
- `wrap_html_body` is superseded by the renderer's `<body>` wrapping and can be
  removed if no longer referenced.

### 5. Conventions (documented, referenced from `editing-tasks` skill)

Written into the standard doc and summarized in the skill so the agent path
follows them:

- **Priority** — expressed only on automated (email-derived) tasks, via the
  `[P0–P3]` title prefix, driven by inbox's classified `importance`. Manual
  tasks carry no priority label; urgency on a manual task is expressed with a
  due date and/or a topic tag, not an invented priority.
- **Tags** — kebab-case topic/context words (`finance`, `passport-renewal`),
  not status or priority (those have their own fields/mechanisms). Tags are
  resolved by name and cached (`services/tags.py`); the convention keeps ad hoc
  tags from drifting into near-duplicates. Not code-enforced.
- **Due dates** — set `due_on` when a concrete calendar date is known; use
  `due_at` only when time-of-day genuinely matters. Do not fabricate a deadline
  when none exists (matches automated behavior: deadline extraction returns
  null absent an explicit date).
- **Comments** — free-form text/`html_text`, unchanged. A comment is a terse,
  single-purpose status update on the task timeline ("waiting on Alice"), not a
  restatement of the description. No source prefix: agent-authored comments are
  already distinguishable via `is_editable` (surfaced by `fetching-task`), so a
  `[Claude Code]` prefix would duplicate an existing signal.

## Affected files

- `models/task_content.py` — **new** (TaskContent dataclass).
- `services/task_content.py` — **new** (render_html_notes; absorbs `_html_notes`,
  `_esc`, action-URL construction).
- `clients/asana.py` — remove `_html_notes`/`_esc`/action-URL helper; `create_task`
  takes rendered `html_notes` rather than building it.
- `handlers/task_create.py` — build `TaskContent` from event + enrichment, render,
  pass down.
- `api/routers/tasks.py` — restructure `CreateTaskRequest`/`UpdateTaskRequest`;
  build+render TaskContent; drop `wrap_html_body` if unused.
- `~/.claude/skills/editing-tasks/SKILL.md` — new request shape + conventions
  summary.
- `docs/` — the task content standard doc (this design's conventions section,
  promoted to a reference doc).
- Tests: `tests/test_task_create.py`, `tests/test_api_tasks.py`, plus new
  `tests/test_task_content.py` for the renderer.

## Testing

- **Renderer unit tests** (`test_task_content.py`): section ordering, each
  section omitted when empty, HTML escaping, mandatory source line appears iff
  metadata empty, `<body>` wrapping.
- **Pipeline test** (`test_task_create.py`): updated to assert the handler builds
  the expected TaskContent / rendered notes (metadata rows, reasoning, key
  points, action links) rather than asserting on the old `_html_notes` output.
- **API test** (`test_api_tasks.py`): create/patch with structured fields produce
  the expected `html_notes`; empty-content create yields the source line;
  patch without content fields leaves the description untouched.

## Risks

- **Breaking API change.** Any external caller of `tasks-api` posting
  `description`/`html_description` breaks. Mitigation: the only known consumers
  are the `editing-tasks` skill and repo tests, both updated in the same change;
  the API token is private to this setup.
- **Pipeline regression.** The automated task appearance must not degrade.
  Mitigation: the renderer reproduces the existing section set; pipeline test
  pins the output.

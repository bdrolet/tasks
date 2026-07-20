# Task Content Standard — Design

**Date:** 2026-07-20
**Status:** Revised (scannability reframe, free-tier native slots) — pending review

## Goal

Make tasks **scannable**: a reader opening any task should know where to look
for each piece of information, because the same kind of information is always
in the same place. Consistency is the means; predictable placement is the end.

The insight that drives the design: "where to look" is two problems, and they
have different best answers.

- **Structured attributes** (priority, category, topic, due date, assignee,
  origin) are the *same kind of value* on every task. For these, "always in the
  same place" is best served by a **dedicated slot** — not a line of prose the
  reader has to parse.
- **Freeform substance** (summary, key points, links, next action) is prose.
  Here a **fixed template** — same sections, same order — is what makes it
  scannable.

The current code conflates the two: it crams structured attributes
(From / Received / Importance / Tags) into a bulleted header *inside* the
description, so a reader still parses prose to find the due date or priority.
This design separates them.

## Problem

Tasks land in the configured Asana project (`ASANA_PROJECT_ID`) from two
independent paths with no
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
tasks are inconsistent with pipeline tasks and with each other.

Two structural problems get fixed along the way:

- `_html_notes()` is business logic (content shaping) living in `clients/`,
  which the repo's layer rules reserve for I/O only. It moves to `services/`.
- The manual API accepts an opaque description blob, so the structure is
  unenforceable there. It becomes structured fields the API assembles.

## Constraint: free Asana tier

The workspace is on the free/Personal Asana tier (confirmed; corroborated by
the 402 on the search endpoint noted in `services/task_search.py`). **Custom
fields are not available.** The structured slots we have are: the title, native
due date, native assignee, native tags, completion, and sections. Everything
else must live in the description. The design uses those native slots for the
structured attributes and reserves the description for prose plus the one thing
that has no native home (origin/provenance).

## Where each piece of information lives

| Information | Slot | Why it's the scannable slot |
|---|---|---|
| Priority (P0–P3) | Title prefix `[P1] …` | Visible in every list/board view without opening the task |
| Category (urgent/respond/review) | **Section** (already done by `services/sections.py`) | Asana groups the board by section — same place every time |
| Topic | **Native tags** (chip row) | Consistent chips, filterable |
| Due date | Native `due_on` / `due_at` | Dedicated UI slot |
| Assignee | Native assignee | Dedicated UI slot |
| Origin / provenance (email From/Received; "created manually") | Description **footer** | No native slot on the free tier — but always the same place |
| Substance (summary, links, next action, reasoning) | Description **template** | Prose needs a predictable skeleton |

## Design

### 1. Shared content model — `models/task_content.py`

A pure dataclass, no imports from other layers (per layer rules):

```python
@dataclass
class TaskContent:
    context: str | None = None                                        # optional lead prose ("what / why")
    key_points: list[str] = field(default_factory=list)               # the substance
    links: list[tuple[str, str]] = field(default_factory=list)        # (url, label)
    action_items: list[tuple[str, str]] = field(default_factory=list) # (label, url) — e.g. confirm/review links
    source: Source | None = None                                      # origin footer
```

```python
@dataclass
class Source:
    origin: str                                # e.g. "Created manually", "Email"
    rows: list[tuple[str, str]] = field(default_factory=list)  # (label, value): From, Received, …
    links: list[tuple[str, str]] = field(default_factory=list) # (url, label): "Open in Outlook"
    note: str | None = None                    # freeform provenance note (automated: AI reasoning)
```

Field meanings:

- **context** — an optional lead paragraph ("what is this / why"). Omitted when
  empty. Manual tasks may use it; the automated path leaves it empty (its
  substance is `key_points`).
- **key_points** — bullet list of specifics (actions requested, facts).
- **links** — labelled hyperlinks.
- **action_items** — labelled hyperlinks under an "Actions" heading; the
  pipeline uses these for the confirm/review/reference/ignore label links.
- **source** — the provenance footer (see rendering order). The structured
  attributes (priority, category, topic, due) are **not** in this model — they
  live in native Asana slots, set by the caller/handler, not rendered into notes.

### 2. HTML renderer — `services/task_content.py`

```python
def render_html_notes(content: TaskContent) -> str: ...
```

- Replaces `clients/asana.py::_html_notes()`. HTML escaping (the existing
  `_esc` behavior — `&`, `<`, `>`) moves here.
- **Section order:** `context → key_points → links → action_items → source`
  (substance first, provenance last — a reader scans the substance, then the
  origin). Each section is omitted when its field is empty, **except** the
  source footer.
- Wraps output in the `<body>…</body>` root Asana requires (same requirement
  `api/routers/tasks.py::wrap_html_body` handles today).
- **Mandatory source footer:** if `content.source` is `None`, the renderer
  emits `Source: Created manually`. The automated pipeline always supplies an
  explicit `Source` (origin "Email", From / Received rows, the Open-in-Outlook
  link, reasoning as the note), so the default only appears on manual tasks that
  pass none. This guarantees every task states where it came from, always in the
  same place.

**Rendering is code, never an LLM.** Determinism is the whole point of a
standard — a reader can only rely on "always in the same place" if the layout
is byte-for-byte identical every time. A model constrained to a schema still
makes per-call choices about what goes where; code does not. The renderer is
pure (TaskContent in, HTML string out), no I/O, unit-testable in isolation.

### 3. Title and priority

- **Automated:** `[{importance}] {subject or '(no subject)'}` — unchanged.
  Importance is a real classified field from inbox.
- **Manual:** the API gains an **optional** `priority` field (`P0`–`P3`). When
  set, the router prefixes the title the same way (`[P1] …`); when omitted, the
  title is plain. This lets a manual caller express priority when it's
  meaningful without forcing a P-level guess onto every ad hoc task.
- Importance is **no longer** rendered as a description metadata line — the title
  prefix is its slot.

### 4. Automated pipeline changes

- `handlers/task_create.py` builds a `TaskContent` from the event + enrichment:
  `key_points` and `links` from the summary, `action_items` from the
  category-dependent label links, and a `Source` carrying origin "Email",
  From / Received rows, the Open-in-Outlook link, and the classifier reasoning
  as the note. The `To`/`Cc`/`Importance`/`Tags` header rows are dropped from the
  description — importance is the title prefix, tags are native tags.
- `clients/asana.py::create_task` stops calling `_html_notes()`; the handler
  renders the notes and passes the string down. The action-link URL construction
  (`action_url`, webhook token) moves to the handler/service layer with the rest
  of the content shaping — it is not I/O and does not belong in the client.
- Title unchanged. Net effect: `clients/asana.py` shrinks to I/O only;
  `_html_notes`, `_esc`, and the action-URL helper leave the client.

### 5. Manual API changes — `api/routers/tasks.py`

**Breaking change to the request bodies** (blast radius is fully contained —
consumers are the `editing-tasks` skill doc and this repo's tests/scripts only):

`CreateTaskRequest` and `UpdateTaskRequest` drop `description` and
`html_description`. They gain structured content fields mirroring `TaskContent`
(`context`, `key_points`, `links`, `action_items`) plus the optional `priority`
above. Tags, due date, assignee, section, and completion are **unchanged** —
they already map to native slots and are the standard's structured slots.

- The router builds a `TaskContent` from the content fields and calls
  `render_html_notes` to produce `html_notes`.
- On **create**, content fields are optional; an all-empty create yields a task
  whose description is just the `Source: Created manually` footer.
- On **update (PATCH)**, presence of any content field means "rewrite the
  description from these fields" (replace, not merge — a description update
  replaces). Content fields absent → description untouched (the same
  `model_fields_set` semantics the PATCH handler already uses for nullable
  fields).
- `wrap_html_body` is superseded by the renderer's `<body>` wrapping and can be
  removed if no longer referenced.

### 6. Conventions (documented, referenced from `editing-tasks` skill)

Written into a standard doc and summarized in the skill so the agent path
follows them:

- **Priority** — expressed via the `[P0–P3]` title prefix. Automatic on
  email-derived tasks (from inbox's `importance`); optional on manual tasks.
  Not a description line, not a tag.
- **Tags** — kebab-case topic/context words (`finance`, `passport-renewal`),
  **not** status or priority (those have their own slots). Tags are resolved by
  name and cached (`services/tags.py`); the convention keeps ad hoc tags from
  drifting into near-duplicates. Not code-enforced.
- **Due dates** — set `due_on` when a concrete calendar date is known; use
  `due_at` only when time-of-day genuinely matters. Do not fabricate a deadline
  when none exists (matches automated behavior: deadline extraction returns null
  absent an explicit date).
- **Category** — expressed by section placement (`services/sections.py`), not by
  a description line. Automatic on the pipeline; manual callers pass `section`.
- **Comments** — free-form text/`html_text`, unchanged. A comment is a terse,
  single-purpose status update on the task timeline ("waiting on Alice"), not a
  restatement of the description. No source prefix: agent-authored comments are
  already distinguishable via `is_editable` (surfaced by `fetching-task`), so a
  `[Claude Code]` prefix would duplicate an existing signal.

## Optional follow-up: structured outputs for email enrichment

Not part of the core standard — a reliability improvement for the automated
path's enrichment, shippable in the same PR or later.

`services/email_summary.py` currently asks Haiku for `{"key_points": [...]}`,
then strips code fences with a regex, `json.loads`es the result, and silently
falls back to empty key points on any exception. Migrating that call to
**structured outputs** (`client.messages.parse()` / `output_config.format`)
constrains the model's JSON to a schema, removing the fence-stripping and the
silent-empty failure mode. Haiku 4.5 (the model this call uses) supports
structured outputs, so no model change is needed.

Scope notes:

- Structured outputs feeds the **content model**, it does not replace the
  renderer — messy email → schema-valid `TaskContent` object → code renders. The
  standard's consistency still comes entirely from the code renderer.
- The deadline extraction in `services/deadline.py` is out of scope: it returns
  a single scalar (an ISO date or `null`), and its model (Sonnet 4.6) is not on
  the structured-outputs supported list. Leave it as-is.

## Non-goals

- **No Asana custom fields** — unavailable on the free tier. If the workspace is
  ever upgraded, priority/category/origin become natural custom-field candidates
  (sortable, filterable), and this standard would migrate them out of the
  title/section/footer. Out of scope now.
- **No LLM in rendering** — the renderer is deterministic code (see §2).
- **No change** to task search, fetch, completion, escalation, or webhook flows.
- **No forced tag vocabulary or tag validation in code** (convention only).
- **No comment schema change** (convention only).
- **No database schema change.**

## Affected files

- `models/task_content.py` — **new** (`TaskContent`, `Source`).
- `services/task_content.py` — **new** (`render_html_notes`; absorbs
  `_html_notes`, `_esc`, action-URL construction).
- `clients/asana.py` — remove `_html_notes`/`_esc`/action-URL helper;
  `create_task` takes rendered `html_notes` rather than building it.
- `handlers/task_create.py` — build `TaskContent` (incl. `Source`) from event +
  enrichment, render, pass down.
- `api/routers/tasks.py` — restructure `CreateTaskRequest`/`UpdateTaskRequest`
  (structured content fields + optional `priority`); build+render `TaskContent`;
  drop `wrap_html_body` if unused.
- `services/sections.py` — **no change** (already maps category → section).
- `services/tags.py` — **no change** (tags stay the topic slot).
- `~/.claude/skills/editing-tasks/SKILL.md` — new request shape + conventions
  summary.
- `docs/` — the task content standard doc (the conventions section, promoted to
  a reference doc).
- Tests: `tests/test_task_create.py`, `tests/test_api_tasks.py`, plus new
  `tests/test_task_content.py` for the renderer. `scripts/test-api-local.py`
  posts name-only, so it still works under the new schema — no change needed.

## Testing

- **Renderer unit tests** (`test_task_content.py`): section ordering
  (context → key_points → links → action_items → source), each non-source
  section omitted when empty, HTML escaping, mandatory source footer appears
  (default `Created manually` when `source` is None; explicit rows/links/note
  when supplied), `<body>` wrapping, byte-identical output for identical input.
- **Pipeline test** (`test_task_create.py`): the handler builds the expected
  `TaskContent` (key points, action links, Source with From/Received/Outlook +
  reasoning) rather than asserting on the old `_html_notes` output; importance is
  the title prefix and not a description line.
- **API test** (`test_api_tasks.py`): create/patch with structured content fields
  produce the expected `html_notes`; empty-content create yields the source
  footer; patch without content fields leaves the description untouched; optional
  `priority` produces the `[P1]` title prefix.

## Risks

- **Breaking API change.** Any external caller of `tasks-api` posting
  `description`/`html_description` breaks. Mitigation: the only known consumers
  are the `editing-tasks` skill and repo tests/scripts, all updated in the same
  change; the API token is private to this setup.
- **Pipeline regression.** The automated task appearance changes (attributes
  leave the description for the title/tags/section/footer). Mitigation: the
  pipeline test pins the new output; the change is deliberate, not incidental.

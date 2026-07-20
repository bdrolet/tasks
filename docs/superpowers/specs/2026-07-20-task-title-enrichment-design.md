# Task Title Enrichment — Design

**Date:** 2026-07-20
**Status:** Pending review

## Goal

Turn pipeline-generated task titles from a passthrough of the email subject
into a **verb-first action title** — so a reader scanning the Asana list knows
what to *do*, not merely what the email was *about*.

Today `clients/asana.py::create_task` sets:

```python
"name": f"[{event['importance']}] {event['subject'] or '(no subject)'}"
```

This inherits every pathology of email subjects: `Re:`/`Fwd:`/list-tag noise,
the sender's framing rather than the reader's next action, and topics
("Q3 board deck") in place of actions ("Review Q3 board deck"). The research
(GTD and task-management consensus) is unanimous: **a task title without a verb
is not actionable**, and email subjects used verbatim are a known anti-pattern.

## Title standard

The canonical standard (documented for humans; see Documentation below):

> `[PX] {verb} {object}`
>
> - **`[PX]`** — unchanged importance prefix (`P0`–`P3`). Fastest scan signal.
> - **`{verb}`** — imperative, present tense, **context-driven**: the model
>   picks the most accurate verb from the email body (not a fixed
>   category→verb map).
> - **`{object}`** — 2–5 words naming what the action is on, specific enough to
>   disambiguate from sibling tasks. Sender pleasantries, `Re:`/`Fwd:`, and list
>   tags are dropped.
> - **Length:** ≤ ~8 words / ~60 chars after the prefix, so the title never
>   truncates in Asana's list view.
> - **Casing:** sentence case, no trailing punctuation.

Examples:

| Category | Raw subject | Enriched title |
|---|---|---|
| respond | `Re: FW: contract redlines` | `[P1] Reply to Sarah on contract redlines` |
| review  | `Q3 board deck — please review by EOD` | `[P0] Review Q3 board deck` |
| urgent  | `URGENT: prod db at 95% disk` | `[P0] Resolve prod DB disk alert` |

**Why context-driven verb (not category-anchored):** decided during
brainstorming — the object alone isn't enough; the verb carries most of the
"what do I do" signal and a fixed category map ("respond"→"Reply") loses
precision (a `respond` email may actually need "Confirm", "Decline", "Send").

**No non-actional fallback needed:** `services/policy.py::warrants_task` only
promotes `urgent`/`review`/`respond` to tasks — all three are inherently
actions. Non-actional categories (`reference`, `ignore`) never reach title
generation, so every generated title is a real action. There is no "soft verb"
case to design.

## Where the enrichment runs

**Fold the title into the existing `services/email_summary.py` Haiku call**
(chosen over a dedicated second call).

`email_summary.generate()` already sends Haiku the subject, sender, and body and
parses a JSON result (`{key_points: [...]}`). We extend that same call to also
return `title`, yielding `{key_points, title}` from one round-trip.

Rejected alternative — a new `services/task_title.py` with its own Haiku call:
cleaner isolation, but a **second model call per task on identical input**
(same body, same model), doubling latency and spend in the hot path of every
created task for a small separation-of-concerns gain.

## Architecture & data flow

```
handlers/task_create.py
  └─ email_summary.generate(event)  →  EmailSummary(key_points, relevant_links, title)
                                           │
        title computed here (fallback applied)
                                           ▼
  └─ asana.create_task(event, ..., title=<enriched or None>)
                                           │
     clients/asana.py::create_task uses `title` if given,
     else falls back to `[{importance}] {subject}`
```

Component changes:

1. **`models/events.py`** — widen the `EmailSummary` dataclass with
   `title: str | None = None`.

2. **`services/email_summary.py`** — extend the Haiku prompt to request a
   `title` field alongside `key_points`, per the title standard. Parse it out of
   the same JSON. The title *guidance* (the standard, as prompt text) lives here
   as a named constant so it is testable and greppable — business logic in
   `services/`, never in `clients/claude.py` (which stays pure I/O).
   - Post-process: prepend the `[PX]` prefix in the handler (not the model), so
     the model only produces `{verb} {object}` and can't mangle the priority
     token. Strip trailing punctuation; enforce a max length (truncate on word
     boundary as a guard, though the prompt already constrains length).

3. **`handlers/task_create.py`** — after `email_summary.generate()`, build the
   final title: `f"[{importance}] {summary.title}"` when `summary.title` is
   present, else `None` (let the client apply the deterministic fallback). Pass
   it to `create_task(..., title=...)`. This mirrors how the handler already
   pre-renders `html_notes` and hands it to the client.

4. **`clients/asana.py::create_task`** — add a keyword arg
   `title: str | None = None`. Use it for `payload["name"]` when given;
   otherwise keep the current `[{importance}] {subject}` expression as the
   fallback. Keeps `clients/` free of title *logic* (it only holds the
   last-resort default) and the enrichment in `services/`/`handlers/`.

The manual/API path (`create_task_from_fields`) is unaffected — callers there
already supply `name` directly.

## Error handling

Title enrichment is **best-effort**, consistent with the repo's stance that a
degraded enrichment must never block a task:

- If the Haiku call fails or returns no/garbled `title`, `email_summary` returns
  `title=None` (same `try/except` that already guards `key_points`).
- `handlers/task_create.py` then passes `title=None`, and `create_task` falls
  back to `[{importance}] {subject}` — today's exact behavior.
- The `[PX]` prefix is always applied deterministically in code, so a bad model
  response can never corrupt the priority signal.

No new failure mode blocks task creation; worst case is a title no better than
today's.

## Testing

- **Unit — `services/email_summary`:** mock `claude.summarize` to return
  `{key_points, title}`; assert `title` is parsed onto `EmailSummary`. Add a
  case where `title` is missing/empty → `title=None`. Existing summary tests
  stay green (contract widened, not broken).
- **Unit — post-processing:** trailing-punctuation strip, max-length truncation,
  empty/whitespace title → `None`.
- **Unit — `handlers/task_create`:** with a title present, assert
  `create_task` is called with `title="[P0] Review Q3 board deck"`; with
  `title=None`, assert `create_task` receives `title=None`.
- **Unit — `clients/asana.create_task`:** `title` given → used verbatim as
  `name`; `title=None` → falls back to `[{importance}] {subject}`.
- **Runtime E2E (`verifying-pr-locally`):** a real Asana create; confirm the
  rendered title lands as expected (unit tests mock Asana, so only a real create
  proves the name field is accepted).

## Documentation

The user asked for solid, referenced documentation of the standard:

- **`CLAUDE.md`** — a short "Task title standard" note (next to "Task policy" /
  section-mapping notes), stating the `[PX] {verb} {object}` format and pointing
  to this spec as the canonical source.
- **`services/email_summary.py`** module docstring — restate the standard where
  the prompt lives, so the guidance and its implementation sit together.
- **Skills** — reference the standard where task titles are described:
  - `tasks-architecture` (how a task is shaped in the pipeline),
  - `verifying-pr-locally` (what a correct title looks like in E2E output),
  - `editing-tasks` (so manually/agent-created titles follow the same shape —
    even though that path won't auto-generate, the standard should guide it).

Exact wording finalized during implementation; the canonical definition is this
spec, and other locations link to it rather than duplicating the rules.

## Out of scope

- Rewriting titles for the manual/API path automatically (callers supply
  `name`; the standard *guides* them via `editing-tasks` but isn't enforced in
  code here).
- Backfilling/renaming titles on existing tasks.
- Any change to the description (`html_notes`), due date, tags, or sections.
- Switching the title model to Sonnet — Haiku is sufficient and already in the
  path.

# Task content standard

Every task in the configured Asana project follows this shape so a reader
always knows where to look. Structured attributes live in native Asana slots;
the description holds prose in a fixed order.

## Where each thing goes

| Information | Slot |
|---|---|
| Priority (P0–P3) | Title prefix `[P1] …` (automatic on email tasks; optional on manual) |
| Category (urgent/respond/review) | Section |
| Topic | Tags (kebab-case: `finance`, `passport-renewal`) |
| Due date | `due_on` (or `due_at` only when time-of-day matters) |
| Assignee | Native assignee |
| Origin / provenance | Description **footer** (`Source: …`) |
| Substance (summary, links, next action) | Description template |

## Description template (fixed order)

`context → key points → links → actions → source footer`. Sections omit when
empty; the source footer is always present (`Created manually` for manual tasks).

## Conventions

- **Priority** — the `[P0–P3]` title prefix, nothing else. Don't put priority
  in a tag or a description line.
- **Tags** — kebab-case topic words only. Not status, not priority (those have
  their own slots).
- **Due dates** — set only when a real date is known; never fabricate one.
- **Comments** — terse, single-purpose status updates ("waiting on Alice"), not
  a restatement of the description.

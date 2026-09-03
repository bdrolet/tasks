# Due-day digest — one all-day calendar event per day with tasks due

**Date:** 2026-09-03
**Status:** designed, not implemented

## Problem

Due dates live in Asana; the day lives on Google Calendar. Nothing connects
them, so "what is due today" means opening a second app, and nothing on the
calendar grid warns that a given day carries deadlines.

Wanted: for every day that has tasks due, an all-day event on the right
calendar listing those tasks — each title a link to its Asana task, with two
or three bullets of substance and links to the documents the task points at.

## Goals

- One all-day event per (day, calendar) for every day in a rolling window
  that has open tasks due, and no event on days that do not.
- Task title is the link. Bullets are readable without opening Asana.
- Routing: Family Board → **Family** calendar; a task tagged `cheryl` →
  **Ben | Cheryl**; everything else → primary.
- Tracks Asana within minutes for the configured project, within the hour for
  every other project; never creates a duplicate event.
- Nothing here writes to Asana. Nothing here talks to Google directly.

## Non-goals

- **Timed events** (`due_at`). Date only; a task's time-of-day is not carried.
- **Reminders.** The event uses the calendar's default reminder setting.
- **Completed-task history.** A completed task drops off its day's event; a
  day whose last task completes loses its event, today included. (Chosen
  deliberately; the alternative — keep today's event with done items struck
  through — was offered and declined.)
- **A webhook on Family Board** or any other project. The Asana webhook is
  registered on the configured project only; other projects catch up on the
  hourly rebuild. Registering a second webhook needs per-webhook secret
  handling and is a separate change.
- **Past days.** Once a day is in the past its event is left as it was.

## Decisions

### D1 — tasks owns the digest; schedule-api owns the calendar write

The dividing rule holds: task policy (which tasks, which calendar, what the
bullets say) lives here; every calendar write goes through `schedule-api`,
exactly as every mailbox read goes through `inbox-api`. A new
`clients/schedule_api.py` mirrors `clients/inbox_api.py`: bearer-authed HTTP,
`SCHEDULE_API_URL` / `SCHEDULE_API_TOKEN` env vars. The `schedule-api-token`
secret is owned by schedule's terraform and referenced here as a data source,
mounted on the webhook CF only — that is the only CF that runs a rebuild.

Rejected: writing to Google Calendar from this repo (a second OAuth
credential holder, and schedule's dedup/routing invariants bypassed), and
building the digest inside schedule by pulling from tasks-api (task routing
and task-content parsing would move into the calendar repo).

### D2 — schedule-api gains a structured `sections` body (separate PR, schedule repo)

`schedule-api` renders event descriptions from `context` / `key_points` /
`links` as plain text and deliberately has no free-form `description`. A
title that *is* a link needs HTML, which Google Calendar renders in
descriptions. Rather than an escape hatch, the API gains one more structured
field, on both `POST /events` and `PATCH /events/{id}`:

```json
"sections": [
  {"title": "[P1] Renew passport", "url": "https://app.asana.com/0/…/…",
   "points": ["Appointment is 2026-09-12 at the SF passport agency",
              "Bring the DS-82 and two photos"],
   "links": [["https://drive.google.com/…", "DS-82 (filled)"]]}
]
```

When `sections` is non-empty the whole description renders as escaped HTML:
`context` as a leading paragraph, then per section `<b><a href="url">title</a></b>`
(bold title without the anchor when `url` is null) followed by one `<ul>` whose
items are the points and then the links as `<a href>label</a>`; top-level
`key_points` / `links` follow as today but in HTML. With `sections` absent or
empty, rendering is byte-for-byte what it is now, so no existing caller
changes. `sections` joins `CONTENT_FIELDS` so a PATCH carrying it re-renders
the description. `docs/event-content-standard.md` gets a **Digest** paragraph
describing this shape and that it is for machine-built list events, not for
`event-builder`.

### D3 — task set: live Asana listing, rolling 30-day window

A rebuild lists the workspace the way `api/routers/search.py` does: every
unarchived project via `list_project_tasks(only_open=True)`, plus
`list_my_tasks(only_open=True)`, de-duplicated by gid. A digest-specific
opt-fields constant adds `tags.name`, `html_notes`, `modified_at`, and
`memberships.project.gid` to what `SEARCH_OPT_FIELDS` carries. The Asana free
tier has no due-date search endpoint, so the listing is the query.

Kept: tasks with `completed = false` and `due_on` in `[today, today + 30]`,
where *today* is the date in `America/Los_Angeles` (the primary calendar's
zone, resolved from `GET /calendars` — never the CF's UTC clock). Subtasks
appear only if a listing returns them (i.e. assigned to Ben); that is
accepted, not engineered around.

Within a day, order is priority prefix (`[P0]` first, no prefix last) then
name, case-folded.

### D4 — routing, first match wins

1. Any `memberships[].project.gid` equals `ASANA_PROJECT_FAMILY_GID` →
   `CALENDAR_FAMILY_ID`.
2. Any tag whose name case-folds to `cheryl` → `CALENDAR_SHARED_ID`
   ("Ben | Cheryl").
3. Otherwise → `primary`.

A task appears in exactly one event. The three values are terraform
variables in the gitignored `terraform.tfvars` → CF env, the same pattern as
the section GIDs: personal identifiers, never committed to this public repo.
A missing family/shared value logs once and that rule is skipped (falls
through to primary) — the digest still runs.

The `cheryl` tag is an ordinary kebab-case topic tag under the content
standard; no new tag grammar.

### D5 — event shape

- **Calendar:** per D4. **Date:** the due day, all-day (`date`, no `end_date`).
- **Title:** `N tasks due` / `1 task due` — the event content standard's
  `{what} {qualifier}`, no priority prefix.
- **Transparency:** `transparent`, so a deadline day never shows as busy in
  `/freebusy`.
- **Sections:** one per task in D3 order — `title` = the Asana task name
  verbatim (it already carries `[PX]`), `url` = `permalink_url`, `points` =
  the bullets (D6), `links` = the task's Links section (D6).
- No `context`, no top-level `key_points`/`links`, no attendees, no reminders
  override, `send_updates: none`.

### D6 — bullets are Claude-condensed and cached; links are deterministic

**Bullets.** `services/task_bullets.py::condense(task) -> list[str]` makes
one `clients/claude.py::summarize` (Haiku) call with the task name and the
text of its description (HTML stripped via the `_TextOnly` pattern in
`services/triage.py`; Actions buttons and the Source footer removed first so
they never leak into a bullet), asking for 2–3 bullets as JSON, same parse
and fence-strip as `services/email_summary.py`. Result: 2–3 strings, each
≤ ~140 chars; the model is told not to restate the title or the due date.

**Cache.** `task_bullets(task_gid PK, content_hash, bullets JSONB,
updated_at)`; `content_hash = sha256(name + "\n" + html_notes)`. Hit on
equal hash → no call. Miss → call, then upsert. A failed or unparseable call
→ deterministic fallback (first two entries of the description's `Key
points:` list, else the lead context clipped to 140 chars, else no bullets)
and **no cache write**, so it retries next rebuild.

**Cap.** At most `DIGEST_BULLET_CALLS_MAX = 40` Haiku calls per rebuild;
tasks past the cap use the fallback this run and heal on later runs. This
bounds the first run and any bulk edit.

**Links.** Parsed from `html_notes` with an `HTMLParser` that captures
`<a>` tags only inside the `Links:` block — the block the description
standard renders between `Key points:` and `Actions`. Actions links are
label-webhook buttons and Source links are the originating email; neither is
a "doc". De-duplicated by URL, order preserved, cap 5.

Bullets are the only LLM cost; a rebuild over an unchanged workspace makes
zero Claude calls.

### D7 — state, diff, and idempotency

Table:

```sql
CREATE TABLE IF NOT EXISTS due_day_events (
    day          DATE  NOT NULL,
    calendar_id  TEXT  NOT NULL,
    event_id     TEXT  NOT NULL,
    content_hash TEXT  NOT NULL,
    task_gids    JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, calendar_id)
);
```

`calendar_id` is the value sent to schedule-api (`primary`, or the real id).
`content_hash` = sha256 of the canonical JSON of (title, sections).

`services/due_digest.py::plan(desired, stored, today) -> Plan` is pure:
given the desired `{(day, calendar_id): DigestEvent}` and the stored rows,
it returns `creates`, `updates` (hash differs), `deletes` (stored in-window
with no desired counterpart). Rows with `day < today` are never in any list.

`handlers/due_digest.py::rebuild()` executes the plan against schedule-api:

- **create** → `POST /search` on that calendar for that day with
  `all_day: true` and query `tasks due` first; a hit whose title matches
  `^\d+ tasks? due$` is adopted (row written, then treated as an update)
  rather than duplicated. Otherwise `POST /events`, then insert the row.
- **update** → `PATCH /events/{id}?calendar=…` with `title` and `sections`.
  A 404 (event deleted by hand) → delete the row and fall into create.
- **delete** → `DELETE /events/{id}?calendar=…`; 404 is success. Then delete
  the row.
- Any other non-2xx from schedule-api → log, count `asana_digest_errors`,
  continue with the next pair; the rebuild reports partial.

Rows older than 90 days are pruned at the end of a rebuild (table hygiene
only; the events stay).

**DB is required for this feature, not best-effort.** Without the rows the
rebuild cannot tell its own events apart from anything else on the calendar,
so a DB failure **skips the rebuild** (`asana_digest_rebuilds{outcome="db_unavailable"}`)
rather than risk duplicates. This is the one place the "Asana is the source
of truth, DB degrades gracefully" rule does not apply, and the reason is
that the source of truth for *our* events is the calendar, which we can
only address by id.

### D8 — triggers: a 10-minute tick with a dirty flag, not inline work

Asana expects a webhook reply within 10 seconds, so nothing heavy runs in
the webhook request. Instead:

- **Dirty flag.** `handlers/asana_webhook.py::receive` sets
  `digest_state.dirty_at = now()` when a delivery contains a task
  `added`, `deleted`, `removed`, or `changed` on `due_on`, `completed`,
  `name`, or `notes` — the filters already registered. (Adding `tags` to the
  registered filter list via the in-place `PUT /webhooks/<gid>` in
  `docs/asana-webhook-setup.md` makes a `cheryl` tag change re-route within
  minutes instead of within the hour; optional runbook step.)
- **Tick.** One Cloud Scheduler job `tasks-digest`, `*/10 * * * *`, posts
  `POST /digest` on the webhook CF with the existing `tasks_escalate_token`
  bearer (it is the Cloud Scheduler credential for cron routes on that CF;
  docs are updated to say so).
- **Decision.** `/digest` rebuilds when `dirty_at > last_rebuilt_at`, or
  `last_rebuilt_at` is older than 60 minutes, or the request body carries
  `{"force": true}`. Otherwise it returns `{"outcome": "skipped"}` after one
  DB read. A rebuild clears the flag by setting `last_rebuilt_at`; a flag set
  *during* a rebuild is naturally caught by the next tick because
  `dirty_at` is compared, not reset.

```sql
CREATE TABLE IF NOT EXISTS digest_state (
    id               BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    dirty_at         TIMESTAMPTZ,
    last_rebuilt_at  TIMESTAMPTZ
);
```

(Single-row table; the `CHECK (id)` idiom keeps it single-row.)

The webhook CF's `timeout_seconds` goes from 120 to 300: a first rebuild is
listing + up to 40 Haiku calls + calendar writes, and the escalation route
already lives on this CF at the same limit class as the events CF.

Concurrency: Cloud Scheduler will not overlap `tasks-digest` runs, and the
tick is the only caller, so no lock is needed. `scripts/test-digest.py` is
for local runs and says so in its docstring.

### D9 — observability

Counters in `clients/otel.py`, all `asana_`-prefixed per the repo rule:

- `asana_digest_rebuilds{outcome=ok|partial|db_unavailable|skipped}`
- `asana_digest_events{op=create|update|delete|adopt}`
- `asana_digest_bullet_calls{result=ok|fallback|capped}`
- `asana_digest_errors{stage=list|bullets|calendar}`

Plus the existing `asana_claude_tokens` from the Haiku calls and
`asana_api_duration` from every Asana call. A span per rebuild with the
window and counts as attributes.

## Components

### tasks repo

| File | Role |
|---|---|
| `clients/schedule_api.py` | HTTP to schedule-api: `create_event`, `patch_event`, `delete_event`, `search`, `calendars`. Raises on non-2xx except where the caller handles 404. |
| `clients/asana.py` | `DIGEST_OPT_FIELDS`; `list_project_tasks` / `list_my_tasks` accept `opt_fields=` override. |
| `services/due_digest.py` | Pure: `today_local()`, `route(task) -> calendar_id`, `window(tasks, today, days=30)`, `order(tasks)`, `build_events(...) -> {(day, cal): DigestEvent}`, `content_hash`, `plan(desired, stored, today)`. No I/O. |
| `services/task_bullets.py` | `condense(task, *, cache, budget) -> (points, links)`: link parser, prompt, fallback, cache logic. Claude is its only I/O. |
| `repo/due_digest.py` | `due_day_events` CRUD, `task_bullets` get/upsert, `digest_state` read/mark. Takes an open connection. |
| `handlers/due_digest.py` | `run(force=False) -> dict`: decision, listing, bullets, plan, execute, prune, metrics. Called from `main.py` only. |
| `main.py` | `POST /digest` route on the webhook CF, gated by `escalation.is_authorized`. |
| `handlers/asana_webhook.py` | Sets the dirty flag (best-effort DB write, never fails the delivery). |
| `models/digest.py` | `DigestTask`, `DigestEvent`, `Plan` dataclasses. |
| `repo/schema.sql` | `due_day_events`, `task_bullets`, `digest_state`. |
| `terraform/` | `schedule_api_url`, `asana_project_family_gid`, `calendar_family_id`, `calendar_shared_id` variables; `schedule-api-token` data source + IAM + env on the webhook CF; `tasks-digest` scheduler job; webhook CF timeout 300. |
| `scripts/test-digest.py` | Local run: `--days 3 --dry-run` prints the plan; without `--dry-run` writes REAL events. |
| `docs/` | CLAUDE.md "Due-day digest" section; `terraform.tfvars.example` entries; webhook-setup note on the optional `tags` filter. |

### schedule repo (its own PR)

| File | Role |
|---|---|
| `api/routers/events.py` | `Section` model; `sections` on create + patch requests; in `CONTENT_FIELDS`. |
| `services/event_content.py` | `render_description(..., sections)` — HTML path when sections present. |
| `docs/event-content-standard.md` | **Digest** paragraph. |
| `tests/test_event_content.py`, `tests/test_api_events.py` | Rendering, escaping, plain-text path unchanged, PATCH re-render. |

## Data flow

```
Cloud Scheduler (*/10) ──POST /digest──▶ webhook CF ── handlers/due_digest.run
                                                           │
   Asana webhook ──▶ asana_webhook.receive ──▶ digest_state.dirty_at
                                                           │
                    decision (dirty | stale 60m | force) ──┤ skipped → return
                                                           ▼
                    Asana: list projects → list tasks → filter window → route
                                                           ▼
                    task_bullets: cache hit / Haiku (≤40) / fallback ; links parsed
                                                           ▼
                    due_digest.build_events → plan(desired, stored rows)
                                                           ▼
                    schedule-api: search-adopt / POST / PATCH / DELETE per pair
                                                           ▼
                    rows upserted/deleted; last_rebuilt_at = now; metrics
```

## Error handling

- Asana listing failure → rebuild aborts, `outcome=error`, flag stays set,
  next tick retries. Nothing on the calendar changes.
- Bullet failures never abort; they degrade to the fallback for that task.
- Calendar-call failures are per pair; the rebuild finishes the rest and
  reports `partial`. Because `last_rebuilt_at` is still set, the failed pair
  is retried at the next dirty or hourly rebuild (its hash still differs).
- DB failure at the decision step → `db_unavailable`, nothing runs (D7).
- Missing `CALENDAR_*` / `ASANA_PROJECT_FAMILY_GID` → that rule is skipped
  with one warning per rebuild; missing `SCHEDULE_API_*` → rebuild aborts
  before listing.

## Testing

Unit (all mocked, no network):

- `due_digest`: routing precedence (family beats tag beats primary; missing
  env falls through), window edges (today inclusive, day 30 inclusive, day
  31 out, undated out, completed out), ordering (`[P0]` < `[P3]` < no
  prefix, then name), title pluralization, `plan` (create / update on hash
  change / no-op on same hash / delete in-window / never touch past days).
- `task_bullets`: cache hit makes no call; miss calls once and writes;
  failure falls back and does not write; cap stops calls at 40; Actions and
  Source links excluded, Links kept, capped at 5, de-duplicated.
- `handlers/due_digest`: decision matrix (dirty / stale / force / skipped),
  404-on-patch recreates, search-adopt path, partial outcome on a 500,
  db_unavailable skips.
- `asana_webhook`: dirty flag set for the filtered events, not for others,
  and a DB error there does not change the 200.
- `main`: `/digest` 401 without the bearer, 200 with it.

Schedule repo: `render_description` with sections (HTML, escaping of `<`
and `&` in titles/points, null url → bold only), without sections (unchanged
plain text), PATCH with sections re-renders.

Live verification (after both deploys, before merging the tasks PR):
`scripts/test-digest.py --days 3 --dry-run`, then the real run; open the
three calendars and confirm one event per day with linked titles; complete
a task in Asana and confirm its day's event updates within ~10 minutes.

## Rollout

1. Schedule PR: `sections` support; deploy `schedule-api` (its
   `deploy-api.yml` on merge). Nothing here can ship first.
2. `scripts/migrate_db.py` against the tasks DB (three additive
   `CREATE TABLE IF NOT EXISTS`) **before** the tasks merge — `deploy.yml`
   fires on merge to `main`, the same ordering as the recurrence rollout.
3. `terraform.tfvars`: `schedule_api_url`, `asana_project_family_gid`,
   `calendar_family_id`, `calendar_shared_id`. Terraform apply: secret data
   source + IAM, env vars, scheduler job, timeout.
4. Merge the tasks PR. The first tick with `last_rebuilt_at` NULL is a full
   rebuild; expect up to 40 Haiku calls on it and the rest on the next hour.
5. Rollback: pause the `tasks-digest` scheduler job. Events already created
   stay; nothing else runs.

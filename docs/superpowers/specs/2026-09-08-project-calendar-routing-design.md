# Project → calendar routing for the due-day digest

**Date:** 2026-09-08
**Status:** designed, not implemented
**Extends:** `docs/superpowers/specs/2026-09-03-due-day-digest-design.md`
**Related, independent:** `docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md`

Supersedes and replaces two earlier drafts, `2026-09-08-cheryl-board-digest-routing-design.md`
and `2026-09-08-carter-board-design.md`, which described the same change to the
same function from two directions. Their content is carried here in full apart
from the config-shape decision, which D3 settles differently and explains.

## Problem

Two boards are shared with Cheryl (`1214922912953512`), and neither routes its
due-day digest anywhere but `primary`:

- **Cheryl's Board** (`1217168472120921`) — renamed from "Cheryl" on 2026-09-08
  and given the standard Inbox/Today/This Week/Soon/Someday/Waiting On/Done
  sections. The shared board for joint matters generally.
- **Carter Board** (`1218314374339159`) — created 2026-09-08 with the same
  section set. The standing board for 3550 Carter Dr Unit 126 — move-in,
  utilities, HOA, maintenance, repairs, furnishing, appliances, address changes
  — for as long as the place is lived in.

`handlers/due_digest.py::_list_candidates` already sweeps **every** project in
the workspace, so tasks on both boards are in the digest today. They are simply
routed to `primary` by `services/due_digest.py::route`, whose only
shared-calendar trigger is a `cheryl` tag. Nothing applies that tag to a task by
virtue of the project it lives in.

Sharing Carter Board required structure worth recording, because free-tier Asana
refuses `addMembers` on a private project and refuses `privacy_setting` values
other than `private`: the board lives in a new `Carter` team
(`1218312938267367`), Cheryl was invited to the organization as a guest and
added to that team, and only then did explicit project membership take. A future
shared board needs the same three steps in the same order.

There is also a shape problem. With Family, Cheryl's Board and Carter Board,
project → calendar routing has three rules. The one-variable-pair-per-project
shape that served one rule stops paying at three.

The request that produced the first of the superseded drafts was "anything that
goes on Cheryl's Board will automatically have the `cheryl` tag". That framing
names a mechanism, not an outcome; D1 records why the outcome is served better
without it.

## Goals

- A task on Cheryl's Board or Carter Board lands on the "Ben | Cheryl" calendar,
  whatever path put it there — API, agent, or a drag in the Asana UI.
- The existing `cheryl` tag rule keeps working unchanged, so email-derived tasks
  on Ben's Board are unaffected.
- Adding a fourth board is one config edit, not a code change plus five files.
- Manual creation files household tasks to Carter Board without being told the
  destination each time.
- No behavior change while the new configuration is unset.

## Non-goals

- **Writing the `cheryl` tag onto tasks.** See D1.
- **`repeat:` support on either board.** Recurrence needs a webhook on the
  project. That is `2026-09-08-cross-project-recurrence-design.md`, and nothing
  here depends on it or is depended on by it (D3). A standing household board is
  precisely where recurrence earns its keep — furnace filters, HOA dues, detector
  batteries — so Carter Board will quietly accumulate inert `repeat:` tags until
  that spec ships. That is a known, accepted gap, not a coupling.
- **Pipeline routing.** Email-derived tasks keep landing in `ASANA_PROJECT_ID`.
  Screening does not learn addresses, and nothing here teaches the pipeline to
  file mail about the unit onto Carter Board; moving such a task is a manual or
  agent action.
- **A dedicated Carter calendar.** `CALENDAR_SHARED_ID` is reused. A Carter task
  and a `cheryl`-tagged task reaching the same calendar by two routes is
  intended.
- **Per-project section mapping.** Both boards have a Done section, but nothing
  in this service moves a task there — unchanged. Review/Respond/Urgent remain
  default-project concepts.
- **Retiring or archiving either board.** Carter Board is standing, not a move
  project.

## Decisions

### D1 — Route by project membership; do not write the tag

The `cheryl` tag has exactly one consumer in this repo: the shared-calendar
branch of `services/due_digest.py::route`. Nothing else reads it. So "tag every
task on the board" and "route every task on the board to the shared calendar"
have the same observable effect, and differ only in cost.

Tagging costs an enforcement problem. Four paths put a task on a board:

| path | interceptable here |
|---|---|
| `POST /tasks` with `project` (`api/routers/tasks.py:256`) | yes |
| `PATCH /tasks/{gid}` moving a task (`api/routers/tasks.py:319`) | yes |
| created or dragged in the Asana UI | **no** — the webhook is registered only on `ASANA_PROJECT_ID` |
| the email pipeline | n/a — it only ever creates into `ASANA_PROJECT_ID` |

Covering the third row means either per-project webhooks (a much larger,
separately-designed change) or a reconcile sweep in the 10-minute digest job
that tags whatever it finds untagged. Both buy a tag whose only reader is the
routing function this spec is already changing.

Routing by membership has no such gap: `_list_candidates` sees the boards' tasks
regardless of how they arrived, so all four rows are covered by one rule.
Rejected alternatives are recorded here rather than dropped, because the visible
tag has a secondary value — legibility in the Asana UI — that a future change may
decide is worth the enforcement cost.

### D2 — Rules are an ordered list checked against the membership set

`route` currently loops over `task["memberships"]` looking for the Family gid.
Adding further project rules inside that same loop would make precedence depend
on the order Asana returns memberships in — a task on both Family and Carter
Board would route to whichever membership happened to come first.

Instead, collect the task's project gids into a set once, then check the rules
against it in a configured order:

```python
def route(task: dict, *, project_calendars: list[tuple[str, str]], shared_calendar_id: str) -> str:
    """First match wins: a project with a configured calendar, in configured
    order; then a `cheryl` tag → shared calendar; else primary. A rule whose
    configuration is empty is skipped, so the digest still runs on a partial
    config."""
    gids = {(m.get("project") or {}).get("gid") for m in task.get("memberships") or []}
    for project_gid, calendar_id in project_calendars:
        if project_gid and calendar_id and project_gid in gids:
            return calendar_id
    if shared_calendar_id:
        for tag in task.get("tags") or []:
            if (tag.get("name") or "").strip().casefold() == "cheryl":
                return shared_calendar_id
    return PRIMARY
```

Precedence is an explicit `order` int on each config entry — default 100,
ascending, ties broken by gid — and **not** the JSON object's key order. Key
order is the obvious replacement and it is wrong here: if the config is built
with `jsonencode` over a Terraform map, keys come out lexicographically sorted,
so precedence would silently depend on how the var happens to be declared.

**Family stays ahead of the shared boards** (`order: 10` against `20`). This
preserves today's behavior — the Family rule is checked first now and stays
first — and is the right default besides: a task that reaches the family board is
family business. Carter Board and Cheryl's Board share an `order` because they
resolve to the same calendar, so their relative sequence is unobservable.

The project rules sit ahead of the tag rule. Their relative order is likewise
unobservable, since both resolve to the same calendar id, but keeping every
shared-calendar trigger adjacent reads better than interleaving.

The empty-configuration skip is the existing convention, carried to the new
rules: an unset gid or calendar id disables that rule alone rather than failing
the rebuild.

### D3 — Routing is its own config map, independent of the managed-project set

One new terraform var → CF env var, `ASANA_PROJECT_CALENDARS`, holding a JSON
object keyed by project gid:

```json
{
  "<family gid>": {"calendar": "<family calendar id>", "order": 10},
  "<cheryl gid>": {"calendar": "<shared calendar id>", "order": 20},
  "<carter gid>": {"calendar": "<shared calendar id>", "order": 20}
}
```

`handlers/due_digest.py::_routing` parses the map, drops entries without a
`calendar`, sorts by `(order, gid)`, and passes the resulting ordered list. A
malformed map logs a warning and yields an empty list — every task routes to
primary rather than the rebuild failing. The same fail-soft reasoning as the
existing per-var warning: a deploy that ships the code before the repo variable
is set degrades to today's behavior rather than erroring.

Threaded exactly as `asana_project_family_gid` is today:

| file | change |
|---|---|
| `terraform/variables.tf:101` | new `asana_project_calendars` variable, defaulting to `"{}"`; delete `asana_project_family_gid` and `calendar_family_id` |
| `terraform/cloud_functions.tf:19` | `ASANA_PROJECT_CALENDARS = var.asana_project_calendars`; drop the two retired lines |
| `handlers/due_digest.py:84` | `_ROUTING_ENV` becomes `ASANA_PROJECT_CALENDARS` + `CALENDAR_SHARED_ID` |
| `scripts/fetch-env.sh:26` | `ASANA_PROJECT_CALENDARS=$(tfvar asana_project_calendars)` |
| `.github/workflows/deploy.yml:50` | `TF_VAR_asana_project_calendars: ${{ vars.ASANA_PROJECT_CALENDARS }}` |

`CALENDAR_SHARED_ID` survives as a standalone var: the `cheryl` **tag** rule
still reads it, and that rule is not project-scoped. `ASANA_PROJECT_FAMILY_GID`
and `CALENDAR_FAMILY_ID` are retired into the map — a breaking config change
that must land in the same apply that sets the new var.

Gids and calendar ids are personal, so the map lives in `terraform.tfvars` and a
GitHub repo variable, never in this repo — the standing rule for every routing
id (CLAUDE.md → Due-day digest).

**Two alternatives were rejected.**

*A discrete variable pair per board* (`ASANA_PROJECT_CHERYL_GID`,
`ASANA_PROJECT_CARTER_GID`, …) is what the first draft proposed, and it is what
the code does for Family today. It works, and at one rule it was the right size.
At three it means five files touched per board and a hardcoded rule sequence in
the function signature, which is what forced D2's ordering question in the first
place.

*Folding `calendar` into `ASANA_MANAGED_PROJECTS`* — the map introduced by
cross-project-recurrence D2 — is what the second draft proposed, on the reasonable
argument that describing one project across several unrelated config vars is
worse than describing it once. It is rejected because it blocks a ~50-line
routing change on an unimplemented project that adds a database table, a
reconciler, per-project webhook secrets and a scheduler job. The two maps also
answer genuinely different questions — *is a webhook registered and are
completions handled* versus *where does this task's day-event go* — and a project
can reasonably be in one and not the other. The second draft's own example map
had `calendar` absent on half its entries.

The cost is two vars that list overlapping gids. That is accepted. If the pair
later proves annoying to keep in sync, merging them is a config migration with
no change to `route`, and this decision is the one line to revert.

### D4 — The board-selection rule lives in the task-builder agent

`POST /tasks` and `PATCH /tasks/{gid}` already accept an arbitrary `project`
(`api/routers/tasks.py`), and commit `2fee80d` already requires the agent to
send one explicitly. Manual targeting therefore needs no service change — only a
rule for choosing.

`.claude/agents/task-builder.md` gains, in the `project` / `section` bullet: a
task tied to 3550 Carter Dr — the unit itself, its utilities, HOA, maintenance,
repairs, furnishing, appliances, or address changes — goes to Carter Board.
Household matters not tied to the place keep their existing homes.

The `cheryl` tag rule is left exactly as written. Its meaning stays "this
involves Cheryl", not "this is on a shared board" — the tag is what still routes
a task correctly after it is dragged off a shared board, and overloading it to
mean board membership would make membership and tag two names for one fact.

### D5 — Routing a board's tasks makes their digest visible to Cheryl

Recorded rather than assumed: both boards are shared with Cheryl, and the digest
events this change produces land on a calendar she can see, carrying each task's
title and its 2–3 Haiku-condensed bullets. That is the point of the change and
is intended.

Note the narrower scope than the equivalent note in cross-project-recurrence D2:
routing changes only *which calendar* a digest event lands on. These boards'
tasks already flow through `_list_candidates` into the digest and its bullet
cache today. What they do **not** yet do is reach the `tasks` DB or the
`task_index` semantic corpus, which is a consequence of managing a project for
webhooks, not of routing it.

## Tests

`tests/test_due_digest.py`, alongside the existing `route` cases:

- a task on Cheryl's Board routes to the shared calendar
- a task on Carter Board routes to the shared calendar via the same mechanism
- Family beats both shared boards when a task is in two, by `order` and not by
  dict iteration — asserted with the rules list built in reverse-gid sequence so
  a regression to key order fails the test
- an entry without a `calendar` never matches
- an empty rules list falls through to the tag rule and then to primary
- the tag rule is skipped when `shared_calendar_id` is empty
- the existing tag cases still pass with the new keyword arguments

`tests/test_due_digest_handler.py`: `_routing` drops calendar-less entries, sorts
by `order`, and returns an empty list for a malformed map without raising.

`route` stays keyword-only, so a missed call site is a collection error rather
than a silent default.

## Rollout

1. Merge the code change. No behavior change while `ASANA_PROJECT_CALENDARS` is
   unset — every task routes by tag or to primary, and Family's routing is off
   until step 2, so steps 1 and 2 must not be separated by long.
2. Set `asana_project_calendars` in `terraform.tfvars` and as the matching
   GitHub repo variable, with all three entries, and delete
   `asana_project_family_gid` / `calendar_family_id` from both. Carter Board's
   entry is `{"calendar": "<shared>", "order": 20}`.
3. `terraform apply` to push the CF env var.
4. The next scheduler tick (≤10 min) rebuilds. Tasks due on either shared board
   move from the primary calendar to "Ben | Cheryl".
5. Update `.claude/agents/task-builder.md` per D4.

Step 4 is a move, not a duplication: `plan()` diffs desired events against
`due_day_events` rows keyed by `(day, calendar_id)`, so the primary-calendar rows
for those days become deletes and the shared-calendar rows become creates in the
same pass. Days that also hold unrouted tasks keep a primary event with those
tasks only.

Steps 1–3 are the breaking-config unit and revert together.

## Open items

- **Nothing blocks implementation.** Cross-project recurrence is independent and
  may land before or after.
- **The visible-tag question (D1) is deferred, not resolved.** If the tag turns
  out to matter in the Asana UI, the cheapest route to it is API-side enforcement
  in `api/routers/tasks.py` plus a sweep, and this routing rule stays regardless.
- **Cheryl's Board and Carter Board overlap.** Both are shared with Cheryl and
  both route to the same calendar. The boundary — Cheryl's Board for joint
  matters generally, Carter Board for the place — is a convention, not something
  the service enforces. Worth revisiting if tasks routinely land on the wrong one.
- **Sections beyond Done.** Carter Board carries the full Ben's Board section
  set, but nothing in this service moves a task into Today or Waiting On. Those
  are hand-managed until something says otherwise.
- **`repeat:` is inert on both boards** until cross-project recurrence ships.
  Carter Board is the board most likely to attract recurring tasks, so the gap is
  worth watching.

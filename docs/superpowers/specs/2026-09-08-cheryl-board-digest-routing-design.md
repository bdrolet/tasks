# Cheryl's Board → shared calendar: a project routing rule for the due-day digest

**Date:** 2026-09-08
**Status:** designed, not implemented
**Extends:** `docs/superpowers/specs/2026-09-03-due-day-digest-design.md`

## Problem

Cheryl's Board (`1217168472120921`, renamed from "Cheryl" on 2026-09-08 and
given the standard Inbox/Today/This Week/Soon/Someday/Waiting On/Done
sections) is the shared board. Its tasks should appear on the
"Ben | Cheryl" calendar. They do not.

`handlers/due_digest.py::_list_candidates` already sweeps **every** project in
the workspace, so these tasks are in the digest today — they are simply routed
to `primary` by `services/due_digest.py::route`, whose only shared-calendar
trigger is a `cheryl` tag. Nothing applies that tag to a task by virtue of the
project it lives in.

The request that produced this spec was "anything that goes on Cheryl's Board
will automatically have the `cheryl` tag". That framing names a mechanism, not
an outcome; D1 records why the outcome is served better without it.

## Goals

- A task on Cheryl's Board lands on the "Ben | Cheryl" calendar, whatever
  path put it there — API, agent, or a drag in the Asana UI.
- The existing `cheryl` tag rule keeps working unchanged, so email-derived
  tasks on Ben's Board are unaffected.
- Adding the rule is a config change per environment, not a code change.

## Non-goals

- **Writing the `cheryl` tag onto tasks.** See D1.
- **`repeat:` support on Cheryl's Board.** Recurrence needs a webhook on the
  project; that is `docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md`,
  independent of this.
- **Per-project section mapping.** Cheryl's Board has a Done section, but
  nothing in this service moves tasks there — unchanged.

## Decisions

### D1 — Route by project membership; do not write the tag

The `cheryl` tag has exactly one consumer in this repo: the shared-calendar
branch of `services/due_digest.py::route`. Nothing else reads it. So "tag every
task on the board" and "route every task on the board to the shared calendar"
have the same observable effect, and differ only in cost.

Tagging costs an enforcement problem. Four paths put a task on the board:

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

Routing by membership has no such gap: `_list_candidates` sees the board's
tasks regardless of how they arrived, so all four rows are covered by one
rule. Rejected alternatives are recorded here rather than dropped, because
the visible tag has a secondary value — legibility in the Asana UI — that a
future change may decide is worth the enforcement cost.

### D2 — Rules are an ordered list checked against the membership set

`route` currently loops over `task["memberships"]` looking for the Family gid.
Adding a second project rule inside that same loop would make precedence
depend on the order Asana returns memberships in — a task on both Family and
Cheryl's Board would route to whichever membership happened to come first.

Instead, collect the task's project gids into a set once, then check the rules
against it in a fixed order:

```python
def route(
    task: dict,
    *,
    family_project_gid: str,
    family_calendar_id: str,
    shared_project_gid: str,
    shared_calendar_id: str,
) -> str:
    """First match wins: Family Board → family calendar; Cheryl's Board or a
    `cheryl` tag → shared calendar; else primary. A rule whose configuration
    is empty is skipped, so the digest still runs with a partial config."""
    gids = {(m.get("project") or {}).get("gid") for m in task.get("memberships") or []}
    for project_gid, calendar_id in (
        (family_project_gid, family_calendar_id),
        (shared_project_gid, shared_calendar_id),
    ):
        if project_gid and calendar_id and project_gid in gids:
            return calendar_id
    if shared_calendar_id:
        for tag in task.get("tags") or []:
            if (tag.get("name") or "").strip().casefold() == "cheryl":
                return shared_calendar_id
    return PRIMARY
```

**Family wins over Cheryl's Board** when a task is in both. This preserves
today's behavior — the Family rule is checked first now and stays first — and
is the right default besides: a task that reaches the family board is family
business.

The project rules sit ahead of the tag rule. Their relative order is
unobservable, since both resolve to the same calendar id, but keeping every
shared-calendar trigger adjacent reads better than interleaving.

The empty-configuration skip is the existing convention, carried to the new
rule: an unset gid or calendar id disables that rule alone rather than
failing the rebuild.

### D3 — One new config var, reusing the existing shared calendar id

`asana_project_cheryl_gid` → `ASANA_PROJECT_CHERYL_GID`, threaded exactly as
`asana_project_family_gid` is:

| file | change |
|---|---|
| `terraform/variables.tf` | new variable, defaulting to `""` |
| `terraform/cloud_functions.tf:19` | `ASANA_PROJECT_CHERYL_GID = var.asana_project_cheryl_gid` |
| `handlers/due_digest.py:85` | `"shared_project_gid": "ASANA_PROJECT_CHERYL_GID"` in `_ROUTING_ENV` |
| `scripts/fetch-env.sh:26` | `ASANA_PROJECT_CHERYL_GID=$(tfvar asana_project_cheryl_gid)` |
| `.github/workflows/deploy.yml:50` | `TF_VAR_asana_project_cheryl_gid: ${{ vars.ASANA_PROJECT_CHERYL_GID }}` |

`calendar_shared_id` is reused as-is — the destination calendar is the same
one the tag rule already targets, and a second variable for it would be two
places to keep in sync.

The gid is personal, so it lives in `terraform.tfvars` and a GitHub repo
variable, never in this repo — the standing rule for every routing id
(CLAUDE.md → Due-day digest).

`_routing()` logs a warning for each unset var and skips that rule, so a
deploy that ships the code before the repo variable is set degrades to
today's behavior rather than erroring.

## Tests

`tests/test_due_digest.py`, alongside the existing `route` cases:

- a task on Cheryl's Board routes to the shared calendar
- Family Board beats Cheryl's Board when a task is in both
- the project rule is skipped when either `shared_project_gid` or
  `shared_calendar_id` is empty, falling through to the tag rule and then
  to primary
- the existing tag cases still pass with the new keyword arguments

Every existing `route` call site in the tests gains the two new keyword
arguments; `route` is keyword-only, so a missed call site is a collection
error, not a silent default.

## Rollout

1. Merge the code change (no behavior change while the var is unset).
2. Set `asana_project_cheryl_gid = "1217168472120921"` in `terraform.tfvars`
   and as the `ASANA_PROJECT_CHERYL_GID` GitHub repo variable.
3. `terraform apply` to push the CF env var.
4. The next scheduler tick (≤10 min) rebuilds; tasks due on Cheryl's Board
   move from the primary calendar to "Ben | Cheryl".

Step 4 is a move, not a duplication: `plan()` diffs desired events against
`due_day_events` rows keyed by `(day, calendar_id)`, so the primary-calendar
rows for those days become deletes and the shared-calendar rows become
creates in the same pass. Days that also hold non-Cheryl tasks keep a primary
event with those tasks only.

## Open items

- Nothing blocks implementation. The visible-tag question (D1) is
  deliberately deferred, not resolved: if the tag turns out to matter in the
  Asana UI, the cheapest route to it is API-side enforcement in
  `api/routers/tasks.py` plus a sweep, and this routing rule stays regardless.

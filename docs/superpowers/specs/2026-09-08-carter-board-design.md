# Carter Board — the standing household board for 3550 Carter Dr

**Date:** 2026-09-08
**Status:** designed, not implemented
**Extends:** `docs/superpowers/specs/2026-09-03-due-day-digest-design.md`
**Depends on:** `docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md`
**Supersedes:** D3 of `docs/superpowers/specs/2026-09-08-cheryl-board-digest-routing-design.md`

## Problem

Carter Board (`1218314374339159`) was created on 2026-09-08 with the standard
Inbox/Today/This Week/Soon/Someday/Waiting On/Done sections. It is the standing
board for 3550 Carter Dr Unit 126 — move-in, utilities, HOA, maintenance,
repairs, furnishing, appliances, address changes — for as long as the place is
lived in. It is shared with Cheryl (`1214922912953512`).

Sharing it required structure worth recording, because free-tier Asana refuses
`addMembers` on a private project and refuses `privacy_setting` values other
than `private`: the board lives in a new `Carter` team (`1218312938267367`),
Cheryl was invited to the organization as a guest and added to that team, and
only then did explicit project membership take. A future shared board needs the
same three steps in the same order.

Two gaps follow from the board existing:

1. **Routing.** Its tasks already reach the digest — `_list_candidates` sweeps
   every project — but `services/due_digest.py::route` sends them to `primary`.
   They belong on "Ben | Cheryl".
2. **Recurrence.** A `repeat:` tag is inert outside `ASANA_PROJECT_ID`, and it
   fails silently. A standing household board is precisely where recurrence
   earns its keep — furnace filters, HOA dues, detector batteries — so the board
   would quietly accumulate tags that never fire.

There is also a shape problem. With Family, Cheryl's Board, and now Carter
Board, project → calendar routing has three rules. The one-variable-pair-per-
project shape that served one rule, and that the Cheryl spec's D3 extends to a
second, stops paying at three.

## Goals

- A task on Carter Board lands on the "Ben | Cheryl" calendar, whatever path put
  it there — API, agent, or a drag in the Asana UI.
- `repeat:` fires for tasks on Carter Board.
- Adding a fourth board is one config edit, not a code change plus five files.
- Manual creation files household tasks to Carter Board without being told the
  destination each time.
- No behavior change while the new configuration is unset.

## Non-goals

- **Pipeline routing.** Email-derived tasks keep landing in `ASANA_PROJECT_ID`.
  Screening does not learn addresses, and nothing here teaches the pipeline to
  file mail about the unit onto Carter Board; moving such a task is a manual or
  agent action.
- **A dedicated Carter calendar.** `CALENDAR_SHARED_ID` is reused. A Carter task
  and a `cheryl`-tagged task reaching the same calendar by two routes is
  intended.
- **Writing the `cheryl` tag onto Carter Board tasks.** D1 of the Cheryl spec
  applies unchanged and for the same reasons.
- **Retiring or archiving the board.** It is standing, not a move project.
- **Per-project section mapping beyond Done.** Review/Respond/Urgent remain
  default-project concepts.

## Decisions

### D1 — Carter Board is a managed project, and calendar routing joins that map

`ASANA_MANAGED_PROJECTS` (cross-project-recurrence D2) already defines "managed"
as *webhook registered, completions handled, Done move applied*. Carter Board
must be in it for `repeat:` to fire at all. Rather than describe one project
across three unrelated config vars, entries gain an optional `calendar`:

```json
{
  "<carter gid>":      {"done": "<carter done gid>", "calendar": "<shared>",  "order": 20},
  "<cheryl gid>":      {"done": null,                "calendar": "<shared>",  "order": 20},
  "<family gid>":      {"done": "<family done gid>", "calendar": "<family>",  "order": 10},
  "<ben's board gid>": {"done": "<...>"},
  "<inbox gid>":       {"done": "<...>"},
  "<mediation gid>":   {"done": null}
}
```

`calendar` absent or null means the project has no routing opinion, which is the
current behavior for every board but Family.

This supersedes D3 of the Cheryl spec: `ASANA_PROJECT_CHERYL_GID` is never
introduced, and `ASANA_PROJECT_FAMILY_GID` / `CALENDAR_FAMILY_ID` are retired
into the map. `CALENDAR_SHARED_ID` survives as a standalone var, because the
`cheryl` **tag** rule still reads it and that rule is not project-scoped.

The Cheryl spec's D1 and D2 are kept in full — route by membership rather than
by writing the tag, and evaluate rules against a membership set in a fixed
order. Only the source of the configuration changes.

### D2 — Rule order is an explicit `order` key, because Terraform sorts map keys

The Cheryl spec's D2 fixes precedence by hardcoding the rule sequence in the
function signature. Sourcing rules from a map loses that, and the obvious
replacement — rely on the JSON object's key order — is wrong here: if
`ASANA_MANAGED_PROJECTS` is built with `jsonencode` over a Terraform map, keys
come out lexicographically sorted, so precedence would silently depend on how
the var happens to be declared.

So each entry carries an optional `order` int, default 100, ascending, ties
broken by gid:

```python
def route(task: dict, *, project_calendars: list[tuple[str, str]], shared_calendar_id: str) -> str:
    """First match wins: a managed project with a calendar, in configured order;
    then a `cheryl` tag → shared calendar; else primary. A rule whose
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

`handlers/due_digest.py::_routing` parses the map, drops entries without a
`calendar`, sorts, and passes the resulting ordered list. A malformed map logs a
warning and yields an empty list — every task routes to primary rather than the
rebuild failing.

**Family stays ahead of the shared boards** (`order: 10` against `20`),
preserving today's behavior and the Cheryl spec's reasoning: a task that reaches
the family board is family business. Carter and Cheryl's Board share an `order`
because they resolve to the same calendar, so their relative sequence is
unobservable.

### D3 — The board-selection rule lives in the task-builder agent

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
a task correctly after it is dragged off Carter Board, and overloading it to
mean board membership would make membership and tag two names for one fact.

### D4 — Managing the board routes its contents into the DB, index, and digest

Consistent with cross-project-recurrence D2's privacy note, recorded here rather
than assumed: managing Carter Board means every task event in it flows into the
`tasks` DB, the `task_index` semantic corpus, and the due-day digest. The board
is shared with Cheryl, and the digest events it produces land on a calendar she
can see. Both are intended.

## Tests

`tests/test_due_digest.py`, alongside the existing `route` cases:

- a task on Carter Board routes to the shared calendar
- a task on Cheryl's Board routes to the shared calendar via the same mechanism
- Family beats Carter Board when a task is in both, by `order` and not by
  dict iteration — asserted with the rules list built in reverse-gid sequence so
  a regression to key order fails the test
- an entry without a `calendar` never matches
- an empty rules list falls through to the tag rule and then to primary
- the existing tag cases still pass with the new keyword arguments

`tests/test_due_digest_handler.py`: `_routing` drops calendar-less entries,
sorts by `order`, and returns an empty list for a malformed map without raising.

`route` stays keyword-only, so a missed call site is a collection error rather
than a silent default.

## Rollout

1. cross-project-recurrence ships, establishing `ASANA_MANAGED_PROJECTS` and the
   webhook sync job. Nothing here can land before it.
2. Add the `calendar` and `order` keys; migrate the Family rule off
   `ASANA_PROJECT_FAMILY_GID` / `CALENDAR_FAMILY_ID` and delete both. This is a
   breaking config change and must land in the same apply as step 3.
3. Set the map in `terraform.tfvars` and the matching GitHub repo variable, with
   Carter Board's entry `{"done": "1218307727724585", "calendar": "<shared>",
   "order": 20}`. `terraform apply`.
4. The webhook sync job registers Carter Board, and `repeat:` becomes live there.
5. The next scheduler tick (≤10 min) rebuilds. Tasks due on Carter Board move
   from the primary calendar to "Ben | Cheryl" — a move, not a duplication, for
   the reason the Cheryl spec sets out: `plan()` diffs against `due_day_events`
   rows keyed by `(day, calendar_id)`, so the primary rows become deletes and the
   shared rows become creates in one pass.
6. Update `.claude/agents/task-builder.md` per D3.

## Open items

- **Sequencing.** This spec is blocked on cross-project-recurrence, which is
  itself designed and unimplemented. If Carter deadlines need the shared calendar
  sooner, the fallback is the Cheryl spec's D3 shape — a discrete
  `ASANA_PROJECT_CARTER_GID` rule — carried as a third variable pair and removed
  when the map lands. Taking that path means three one-off rules in the tree at
  once, which is the cost of not waiting.
- **Cheryl's Board and Carter Board overlap.** Both are shared with Cheryl and
  both route to the same calendar. The boundary — Cheryl's Board for joint
  matters generally, Carter Board for the place — is a convention, not something
  the service enforces. Worth revisiting if tasks routinely land on the wrong one.
- **Sections beyond Done.** Carter Board carries the full Ben's Board section
  set, but nothing in this service moves a task into Today or Waiting On. Those
  are hand-managed until something says otherwise.

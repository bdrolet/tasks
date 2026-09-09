# Project → calendar routing for the due-day digest — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route a due-day digest event to a calendar by the task's *project
membership*, driven by one JSON config map, so Cheryl's Board and Carter Board
land on the "Ben | Cheryl" calendar and a fourth board is a config edit.

**Architecture:** `services/due_digest.py::route` stops hardcoding a single
Family-Board rule and instead walks an ordered list of `(project_gid,
calendar_id)` rules against the set of the task's project gids, first match
wins, then falls through to today's `cheryl`-tag rule and then to primary.
`handlers/due_digest.py::_routing` builds that ordered list by parsing a new
`ASANA_PROJECT_CALENDARS` env var (a JSON object keyed by project gid, each
entry `{calendar, order}`), sorting by `(order, gid)` and dropping entries
without a calendar. `ASANA_PROJECT_FAMILY_GID` and `CALENDAR_FAMILY_ID` are
retired into that map; `CALENDAR_SHARED_ID` survives for the tag rule. No task
writes the `cheryl` tag — routing is by membership only (spec D1).

**Tech Stack:** Python 3.13, pytest, ruff (line-length 100), mypy, Terraform
(GCS backend, prefix `tasks`), Cloud Functions Gen2 env vars, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md`

## Global Constraints

- `route` stays **keyword-only** — a missed call site must be a collection
  error, not a silent default (spec → Tests).
- Precedence comes from the explicit `order` int (default `100`, ascending,
  ties broken by gid), **never** from JSON key order or dict iteration order
  (D2).
- Family is `order: 10`; Cheryl's Board and Carter Board are `order: 20` (D2).
- Every rule fails soft: an unset gid, an unset calendar id, or a malformed
  `ASANA_PROJECT_CALENDARS` disables *that rule only* and logs a warning — the
  rebuild never raises on config (D2, D3).
- Real project gids and calendar ids are **personal**: they live in
  `terraform/terraform.tfvars` and GitHub repo variables only, never in a file
  committed to this repo (CLAUDE.md → Due-day digest). Tests and examples use
  placeholders.
- Nothing in this change writes the `cheryl` tag onto a task (D1).
- Every commit leaves `.venv/bin/pytest tests/ -q`, `ruff check .`,
  `ruff format --check .` and `mypy clients/ services/ handlers/ models/ repo/
  api/ main.py` green — that is what CI runs.
- Branch off `main` and open a PR with the `/pr-open` skill; never commit to
  `main` (CLAUDE.md → Development workflow).

## File Structure

| file | responsibility after this change |
|---|---|
| `services/due_digest.py` | pure `route(task, *, project_calendars, shared_calendar_id)` — ordered project rules, then tag, then primary |
| `handlers/due_digest.py` | `_project_calendars()` parses/sorts/filters the JSON map; `_routing()` returns the kwargs `route` takes |
| `tests/test_due_digest.py` | `route` policy cases |
| `tests/test_due_digest_handler.py` | `_project_calendars` / `_routing` config parsing + the existing end-to-end rebuild cases |
| `terraform/variables.tf` | `asana_project_calendars` (string, default `"{}"`); Family pair deleted |
| `terraform/cloud_functions.tf` | `ASANA_PROJECT_CALENDARS` in `common_env`; Family pair deleted |
| `terraform/terraform.tfvars.example` | documented placeholder, including the HCL escaping the value needs |
| `scripts/fetch-env.sh` | `tfvar_json` helper + the new line, so a local `.env` gets valid JSON |
| `.github/workflows/deploy.yml` | `TF_VAR_asana_project_calendars` |
| `main.py`, `CLAUDE.md` | env-var docs |
| `.claude/agents/task-builder.md` | Carter Board selection rule (D4) |

---

### Task 1: `route` takes an ordered list of project rules

Rewrites the routing policy itself. `_routing` is adapted in the same task —
minimally, still reading today's two Family env vars — so the suite stays green
at this commit; Task 2 replaces that adapter with the JSON map. Reviewable on
its own: this task is "is the precedence logic right", Task 2 is "is the config
parsed right".

**Files:**
- Modify: `services/due_digest.py:25-39` (the `route` function)
- Modify: `handlers/due_digest.py:84-95` (`_ROUTING_ENV` / `_routing`)
- Test: `tests/test_due_digest.py:6-8` (the `ROUTE` fixture dict) and
  `tests/test_due_digest.py:34-58` (the five `test_route_*` cases)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `services.due_digest.route(task: dict, *, project_calendars:
  list[tuple[str, str]], shared_calendar_id: str) -> str`. `project_calendars`
  is already in match order — `route` never sorts. Task 2 builds that list.

- [ ] **Step 1: Write the failing tests**

Replace the `ROUTE` dict at the top of `tests/test_due_digest.py` (lines 6–8)
with:

```python
ROUTE = dict(
    project_calendars=[("fam", "cal-fam"), ("cheryl", "cal-shared"), ("carter", "cal-shared")],
    shared_calendar_id="cal-shared",
)
```

Then replace the whole block of route tests (`test_route_family_project_wins_over_tag`
through `test_route_skips_rules_whose_env_is_missing`, lines 34–58) with:

```python
def test_route_family_project_wins_over_tag():
    task = {"memberships": [{"project": {"gid": "fam"}}], "tags": [{"name": "cheryl"}]}
    assert dd.route(task, **ROUTE) == "cal-fam"


def test_route_cheryl_board_to_shared_calendar():
    task = {"memberships": [{"project": {"gid": "cheryl"}}], "tags": []}
    assert dd.route(task, **ROUTE) == "cal-shared"


def test_route_carter_board_to_shared_calendar():
    task = {"memberships": [{"project": {"gid": "carter"}}], "tags": []}
    assert dd.route(task, **ROUTE) == "cal-shared"


def test_route_follows_list_order_not_gid_order():
    # The rules are deliberately in descending-gid sequence: a regression that
    # sorted or iterated by gid — or by Asana's membership order — would hand
    # this task to Carter Board's calendar instead of Family's.
    task = {
        "memberships": [{"project": {"gid": "carter"}}, {"project": {"gid": "fam"}}],
        "tags": [],
    }
    rules = [("fam", "cal-fam"), ("carter", "cal-shared")]
    assert dd.route(task, project_calendars=rules, shared_calendar_id="cal-shared") == "cal-fam"


def test_route_entry_without_calendar_never_matches():
    task = {"memberships": [{"project": {"gid": "carter"}}], "tags": []}
    rules = [("carter", ""), ("", "cal-fam")]
    assert dd.route(task, project_calendars=rules, shared_calendar_id="") == dd.PRIMARY


def test_route_cheryl_tag_case_insensitive():
    task = {"memberships": [{"project": {"gid": "other"}}], "tags": [{"name": "Cheryl"}]}
    assert dd.route(task, **ROUTE) == "cal-shared"


def test_route_default_primary():
    task = {"memberships": [{"project": {"gid": "other"}}], "tags": []}
    assert dd.route(task, **ROUTE) == dd.PRIMARY


def test_route_empty_rules_falls_through_to_tag_then_primary():
    tagged = {"memberships": [{"project": {"gid": "carter"}}], "tags": [{"name": "cheryl"}]}
    untagged = {"memberships": [{"project": {"gid": "carter"}}], "tags": []}
    assert dd.route(tagged, project_calendars=[], shared_calendar_id="cal-shared") == "cal-shared"
    assert dd.route(untagged, project_calendars=[], shared_calendar_id="cal-shared") == dd.PRIMARY


def test_route_tag_rule_skipped_without_shared_calendar():
    task = {"memberships": [], "tags": [{"name": "cheryl"}]}
    assert dd.route(task, project_calendars=[], shared_calendar_id="") == dd.PRIMARY
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_due_digest.py -q`
Expected: FAIL — `TypeError: route() got an unexpected keyword argument
'project_calendars'` on every route case.

- [ ] **Step 3: Rewrite `route`**

In `services/due_digest.py`, replace the existing `route` (lines 25–39) with:

```python
def route(task: dict, *, project_calendars: list[tuple[str, str]], shared_calendar_id: str) -> str:
    """First match wins: a project with a configured calendar, in the order the
    caller supplies; then a `cheryl` tag → shared calendar; else primary. A rule
    whose configuration is empty is skipped, so the digest still runs on a
    partial config. Never sorts — precedence is the caller's (handlers/due_digest
    ::_project_calendars, by the `order` key). Design:
    docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md."""
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

- [ ] **Step 4: Adapt `_routing` so the handler still calls `route` correctly**

In `handlers/due_digest.py`, keep `_ROUTING_ENV` exactly as it is and change
only the return of `_routing` (lines 89–95) to build a one-rule list. This is
temporary scaffolding that Task 2 deletes:

```python
def _routing() -> dict:
    cfg = {key: os.environ.get(env, "") for key, env in _ROUTING_ENV.items()}
    for key, value in cfg.items():
        if not value:
            logger.warning("Digest routing: %s unset — that rule is skipped", _ROUTING_ENV[key])
    return {
        "project_calendars": [(cfg["family_project_gid"], cfg["family_calendar_id"])],
        "shared_calendar_id": cfg["shared_calendar_id"],
    }
```

An unset pair yields `[("", "")]`, which `route` skips — behavior is identical
to today's.

- [ ] **Step 5: Run the full suite and the CI checks**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
Expected: PASS — including the untouched
`tests/test_due_digest_handler.py::test_rebuild_creates_routed_events_and_records_rows`,
which still routes `fam` → `cal-fam` and the `cheryl` tag → `cal-shared`.

- [ ] **Step 6: Commit**

```bash
git add services/due_digest.py handlers/due_digest.py tests/test_due_digest.py
git commit -m "feat(digest): route by an ordered list of project → calendar rules"
```

---

### Task 2: `_routing` parses `ASANA_PROJECT_CALENDARS`

**Files:**
- Modify: `handlers/due_digest.py` — add `import json`, replace `_ROUTING_ENV`
  and `_routing` (lines 84–95) with `_project_calendars` + `_routing`
- Modify: `main.py:24` (env-var docstring line)
- Test: `tests/test_due_digest_handler.py:32-39` (the `env` fixture) and new
  cases appended after `test_should_rebuild_matrix`

**Interfaces:**
- Consumes: `services.due_digest.route(task, *, project_calendars, shared_calendar_id)`
  from Task 1.
- Produces: `handlers.due_digest._project_calendars() -> list[tuple[str, str]]`
  and `handlers.due_digest._routing() -> dict` whose keys are exactly `route`'s
  keyword parameters, so `dd.route(t, **routing)` at
  `handlers/due_digest.py:118` is unchanged.

- [ ] **Step 1: Write the failing tests**

In `tests/test_due_digest_handler.py`, add `import json` to the top imports
(before `import httpx`), and replace the three routing lines in the `env`
fixture (lines 36–38) with:

```python
    monkeypatch.setenv(
        "ASANA_PROJECT_CALENDARS",
        json.dumps(
            {
                "fam": {"calendar": "cal-fam", "order": 10},
                "cheryl": {"calendar": "cal-shared", "order": 20},
            }
        ),
    )
    monkeypatch.setenv("CALENDAR_SHARED_ID", "cal-shared")
```

Then append these cases immediately after `test_should_rebuild_matrix`:

```python
def test_project_calendars_sorts_by_order_and_drops_calendarless(monkeypatch):
    # Keys are deliberately not in `order` sequence, and "boardless" would sort
    # first by both key order and `order` if it were not dropped for having no
    # calendar — a regression to key order fails this test.
    monkeypatch.setenv(
        "ASANA_PROJECT_CALENDARS",
        json.dumps(
            {
                "carter": {"calendar": "cal-shared", "order": 20},
                "boardless": {"order": 5},
                "fam": {"calendar": "cal-fam", "order": 10},
            }
        ),
    )
    assert h._project_calendars() == [("fam", "cal-fam"), ("carter", "cal-shared")]


def test_project_calendars_defaults_order_and_ties_break_by_gid(monkeypatch):
    monkeypatch.setenv(
        "ASANA_PROJECT_CALENDARS",
        json.dumps(
            {
                "zeta": {"calendar": "cal-z"},
                "alpha": {"calendar": "cal-a"},
                "fam": {"calendar": "cal-fam", "order": 10},
            }
        ),
    )
    assert h._project_calendars() == [
        ("fam", "cal-fam"),
        ("alpha", "cal-a"),
        ("zeta", "cal-z"),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json",
        "[]",
        '{"fam": "cal-fam"}',
        '{"fam": {"calendar": "cal-fam", "order": "soon"}}',
    ],
)
def test_project_calendars_unset_or_malformed_yields_no_rules(monkeypatch, raw):
    monkeypatch.setenv("ASANA_PROJECT_CALENDARS", raw)
    assert h._project_calendars() == []


def test_routing_returns_routes_kwargs(env):
    assert h._routing() == {
        "project_calendars": [("fam", "cal-fam"), ("cheryl", "cal-shared")],
        "shared_calendar_id": "cal-shared",
    }


def test_routing_without_shared_calendar_keeps_project_rules(env, monkeypatch):
    monkeypatch.delenv("CALENDAR_SHARED_ID")
    routing = h._routing()
    assert routing["shared_calendar_id"] == ""
    assert routing["project_calendars"] == [("fam", "cal-fam"), ("cheryl", "cal-shared")]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_due_digest_handler.py -q`
Expected: FAIL — `AttributeError: module 'handlers.due_digest' has no attribute
'_project_calendars'`, plus `test_rebuild_creates_routed_events_and_records_rows`
now routing the Family task to `primary` because the old env vars are gone.

- [ ] **Step 3: Replace `_ROUTING_ENV` and `_routing`**

Add `import json` as the first line of the stdlib import block in
`handlers/due_digest.py`, so it reads `json, logging, os, re`. Then replace
lines 84–95 with:

```python
_PROJECT_CALENDARS_ENV = "ASANA_PROJECT_CALENDARS"
_SHARED_CALENDAR_ENV = "CALENDAR_SHARED_ID"
_DEFAULT_ORDER = 100


def _project_calendars() -> list[tuple[str, str]]:
    """ASANA_PROJECT_CALENDARS ({"<project gid>": {"calendar": ..., "order": ...}})
    → [(project_gid, calendar_id)] in match order: ascending `order`, ties by
    gid. Entries without a calendar are dropped. An unset or malformed map logs
    and yields no rules, so a rebuild degrades to tag/primary routing rather
    than failing."""
    raw = os.environ.get(_PROJECT_CALENDARS_ENV, "").strip()
    if not raw:
        logger.warning(
            "Digest routing: %s unset — every project rule is skipped", _PROJECT_CALENDARS_ENV
        )
        return []
    try:
        entries = [
            (int(cfg.get("order", _DEFAULT_ORDER)), gid, str(cfg.get("calendar") or ""))
            for gid, cfg in json.loads(raw).items()
        ]
    except (AttributeError, TypeError, ValueError) as exc:
        logger.warning(
            "Digest routing: %s is malformed (%s) — every project rule is skipped",
            _PROJECT_CALENDARS_ENV,
            exc,
        )
        return []
    return [(gid, calendar) for _, gid, calendar in sorted(entries) if calendar]


def _routing() -> dict:
    """The keyword arguments dd.route takes."""
    shared = os.environ.get(_SHARED_CALENDAR_ENV, "")
    if not shared:
        logger.warning(
            "Digest routing: %s unset — the cheryl tag rule is skipped", _SHARED_CALENDAR_ENV
        )
    return {"project_calendars": _project_calendars(), "shared_calendar_id": shared}
```

The `except` tuple covers every shape the env var can take: a bare list or
scalar (`.items()` → `AttributeError`), a string value where an object is
expected (`.get` → `AttributeError`), bad JSON (`json.JSONDecodeError`, a
`ValueError`), and a non-numeric `order` (`ValueError`/`TypeError`).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_due_digest_handler.py -q`
Expected: PASS, including the pre-existing
`test_rebuild_creates_routed_events_and_records_rows`.

- [ ] **Step 5: Update the env-var docstring in `main.py`**

Replace line 24:

```
  ASANA_PROJECT_FAMILY_GID / CALENDAR_FAMILY_ID / CALENDAR_SHARED_ID — digest routing
```

with:

```
  ASANA_PROJECT_CALENDARS / CALENDAR_SHARED_ID — digest routing (project map, cheryl tag)
```

- [ ] **Step 6: Run the full suite and the CI checks**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add handlers/due_digest.py tests/test_due_digest_handler.py main.py
git commit -m "feat(digest): drive project routing from ASANA_PROJECT_CALENDARS"
```

---

### Task 3: Thread the new var through terraform, the deploy workflow, and local env

Config only — no test cycle beyond `terraform validate` and a grep that the
retired names are gone everywhere.

**Files:**
- Modify: `terraform/variables.tf:101-112` (delete two variables, add one)
- Modify: `terraform/cloud_functions.tf:19-20`
- Modify: `terraform/terraform.tfvars.example:43-50`
- Modify: `scripts/fetch-env.sh:6` (new helper) and `:26-27`
- Modify: `.github/workflows/deploy.yml:50-51`
- Modify: `CLAUDE.md:142-152` (Due-day digest section)

**Interfaces:**
- Consumes: `ASANA_PROJECT_CALENDARS`, the env var Task 2's
  `_project_calendars()` reads. The Terraform variable is a **string** holding
  JSON — not a typed map — so the same literal works as a `TF_VAR_` env var, a
  GitHub repo variable, and a `.env` line.
- Produces: nothing later tasks consume.

- [ ] **Step 1: Replace the two Terraform variables with one**

In `terraform/variables.tf`, delete the `asana_project_family_gid` and
`calendar_family_id` blocks (lines 101–112) and put this in their place, above
the surviving `calendar_shared_id`:

```hcl
variable "asana_project_calendars" {
  description = "Due-day digest project routing, as a JSON object: {\"<project gid>\": {\"calendar\": \"<Google Calendar id>\", \"order\": <int>}}. First match wins by ascending order (ties by gid); entries without a calendar are ignored. Family = 10, shared boards = 20. Personal ids: terraform.tfvars + GitHub repo variable only. \"{}\" disables every project rule."
  type        = string
  default     = "{}"
}
```

- [ ] **Step 2: Swap the CF env vars**

In `terraform/cloud_functions.tf`, replace lines 19–20 (`ASANA_PROJECT_FAMILY_GID`
and `CALENDAR_FAMILY_ID`) with one line, leaving `CALENDAR_SHARED_ID` and
`CHERYL_EMAILS` in place:

```hcl
    ASANA_PROJECT_CALENDARS   = var.asana_project_calendars
```

- [ ] **Step 3: Update the tfvars example**

In `terraform/terraform.tfvars.example`, replace the `asana_project_family_gid`
and `calendar_family_id` lines and refresh the comment above them:

```hcl
# Due-day digest routing (docs/superpowers/specs/2026-09-03-due-day-digest-design.md,
# docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md).
# Project GIDs: the numeric id in the project URL. Calendar ids: run
#   TOKEN=$(gcloud secrets versions access latest --secret=schedule-api-token --project bens-project-462804)
#   curl -s https://schedule-api.drolet.cloud/calendars -H "Authorization: Bearer $TOKEN"
# and copy calendar_id for "Family" and "Ben | Cheryl".
# The value is JSON inside an HCL string, so every inner quote is escaped and the
# whole map is one line — scripts/fetch-env.sh unescapes it into .env. Lower
# `order` wins: Family Board 10, the shared boards 20.
asana_project_calendars = "{\"<family gid>\": {\"calendar\": \"c_family...@group.calendar.google.com\", \"order\": 10}, \"<cheryl board gid>\": {\"calendar\": \"c_shared...@group.calendar.google.com\", \"order\": 20}, \"<carter board gid>\": {\"calendar\": \"c_shared...@group.calendar.google.com\", \"order\": 20}}"
calendar_shared_id      = "c_...@group.calendar.google.com"
```

- [ ] **Step 4: Teach `fetch-env.sh` to unescape the JSON**

The existing `tfvar` helper strips the surrounding quotes but leaves `\"`
escapes, and an unquoted `.env` value containing spaces breaks
`set -a; source .env`. Add a second helper next to `tfvar` (line 6):

```bash
tfvar_json() { tfvar "$1" | sed 's/\\"/"/g'; }
```

and replace the `ASANA_PROJECT_FAMILY_GID` / `CALENDAR_FAMILY_ID` lines in the
heredoc with a single **single-quoted** line (JSON never contains `'`, so the
quoting is safe), keeping `CALENDAR_SHARED_ID` as it is:

```bash
ASANA_PROJECT_CALENDARS='$(tfvar_json asana_project_calendars)'
```

- [ ] **Step 5: Update the deploy workflow**

In `.github/workflows/deploy.yml`, replace the
`TF_VAR_asana_project_family_gid` and `TF_VAR_calendar_family_id` lines with:

```yaml
          TF_VAR_asana_project_calendars: ${{ vars.ASANA_PROJECT_CALENDARS }}
```

`TF_VAR_calendar_shared_id` stays.

- [ ] **Step 6: Update CLAUDE.md**

In the `## Due-day digest` section, replace the first sentence (lines 142–146)
with:

```markdown
One all-day event per day that has open tasks due, for a rolling 30-day
window, on the calendar the task belongs to: project membership first, from the
`ASANA_PROJECT_CALENDARS` map (project gid → `{calendar, order}`; ascending
`order`, first match wins — Family Board → Family, Cheryl's Board and Carter
Board → "Ben | Cheryl"); then a `cheryl` tag → "Ben | Cheryl"
(`CALENDAR_SHARED_ID`); everything else → primary.
```

and extend the section's closing `Design:` line to name both specs:

```markdown
Design: `docs/superpowers/specs/2026-09-03-due-day-digest-design.md`; project
routing: `docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md`.
```

- [ ] **Step 7: Verify the retired names are gone and Terraform is valid**

```bash
grep -rn "ASANA_PROJECT_FAMILY_GID\|CALENDAR_FAMILY_ID\|asana_project_family_gid\|calendar_family_id" \
  clients services handlers repo models api tests scripts terraform .github main.py CLAUDE.md
terraform -chdir=terraform fmt -check
terraform -chdir=terraform validate
.venv/bin/pytest tests/ -q
```

Expected: the grep prints nothing; `fmt -check` and `validate` exit 0; tests
pass. The grep is scoped to code and config on purpose — `docs/` legitimately
still names the retired vars, in both specs (which record the retirement) and
in the previous plan's historical text. Leave every `docs/` hit alone.

If `fmt -check` reports the `common_env` block, run `terraform -chdir=terraform fmt`
and re-run the check.

- [ ] **Step 8: Commit**

```bash
git add terraform/variables.tf terraform/cloud_functions.tf \
  terraform/terraform.tfvars.example scripts/fetch-env.sh \
  .github/workflows/deploy.yml CLAUDE.md
git commit -m "feat(digest): retire the Family env pair into ASANA_PROJECT_CALENDARS"
```

---

### Task 4: Teach the task-builder agent to file Carter Board work

Spec D4. No service change — `POST /tasks` already takes any `project`; what is
missing is the rule for choosing one. The `cheryl` **tag** bullet is left
exactly as written: the tag means "this involves Cheryl", not "this is on a
shared board".

**Files:**
- Modify: `.claude/agents/task-builder.md:105-111` (the `project` / `section` bullet)

**Interfaces:**
- Consumes: nothing. Produces: nothing.

- [ ] **Step 1: Extend the `project` / `section` bullet**

Replace the bullet at lines 105–111 with:

```markdown
- **`project` / `section`** — always send an explicit `project`. Never rely on the
  API default: omitting `project` falls back to the email pipeline's project
  (Inbox), which is almost never where a manual task belongs.
  `curl -s "$BASE/projects" -H "Authorization: Bearer $TOKEN"` lists projects with
  their sections — pick the one the request belongs to. A task tied to 3550
  Carter Dr Unit 126 — the unit itself, its utilities, HOA, maintenance,
  repairs, furnishing, appliances, or address changes — goes to
  **Carter Board**; household matters not tied to the place keep their
  existing homes. The only exception is a subtask, which takes `parent`
  instead (next bullet).
```

Keep `**Carter Board**` on a single line — Step 2's grep checks for it.

- [ ] **Step 2: Verify the symlinked copy resolves**

The agents are symlinked into `~/.claude/` by `scripts/link-skills.sh`, so
editing the repo file is enough. Confirm:

```bash
readlink ~/.claude/agents/task-builder.md
grep -c "Carter Board" ~/.claude/agents/task-builder.md
```

Expected: the readlink points at `/Users/ben/src/tasks/.claude/agents/task-builder.md`
and the grep prints `1`. If `readlink` prints nothing, the agent is a copy, not
a symlink — run `scripts/link-skills.sh` and re-check.

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/task-builder.md
git commit -m "task-builder: file 3550 Carter Dr work on Carter Board"
```

---

### Task 5: Open the PR

- [ ] **Step 1: Confirm the whole suite and CI checks are green**

Run: `.venv/bin/pytest tests/ -q && .venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy clients/ services/ handlers/ models/ repo/ api/ main.py`
Expected: PASS.

- [ ] **Step 2: Open the PR with the `/pr-open` skill**

Body must state that the `ASANA_PROJECT_CALENDARS` GitHub repo variable and
the matching `terraform.tfvars` entry are set *before* this PR merges (spec →
Rollout step 1), so the merge's own auto-deploy applies code and config
together in one pass, with no window where Family Board routing is off.

---

## Rollout (operator — after merge, not part of the coding tasks)

Spec → Rollout. The window between merge and apply is avoidable: the digest
ticks every 10 minutes, and `.github/workflows/deploy.yml` already runs
`terraform apply` on every push to `main` with `TF_VAR_asana_project_calendars`
from the GitHub repo variable. `main` does not yet declare
`asana_project_calendars`, so setting that repo variable ahead of the merge is
inert — Terraform ignores an undeclared `TF_VAR_`. Setting it first means the
merge's own auto-deploy lands code and config in the same apply.

- [ ] **1.** Set the `ASANA_PROJECT_CALENDARS` GitHub repo variable and add
  `asana_project_calendars` to `terraform/terraform.tfvars`, with all three
  entries — Family `{"calendar": "<family calendar id>", "order": 10}`,
  Cheryl's Board (`<cheryl board gid>`) and Carter Board (`<carter board gid>`)
  each `{"calendar": "<shared calendar id>", "order": 20}` — escaped and on one
  line, per the example file. (The actual gids are personal: look them up in
  the design spec, `docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md`,
  or in each board's Asana project URL — never write them into a committed
  file.) The GitHub repo variable takes unescaped JSON, since it is not HCL
  there. Leave the old `ASANA_PROJECT_FAMILY_GID` / `CALENDAR_FAMILY_ID` repo
  variables and `asana_project_family_gid` / `calendar_family_id` tfvars lines
  in place for now — both variables are declared and read independently, so
  having all four present briefly is harmless.
- [ ] **2.** Merge the PR. The auto-deploy runs `terraform apply` with the new
  code and the new variable already set, so there is no interval where Family
  routing is off. A manual `/terraform-plan` + `/terraform-apply` is needed
  only if the auto-deploy did not run.
- [ ] **3.** Delete the retired `ASANA_PROJECT_FAMILY_GID` / `CALENDAR_FAMILY_ID`
  GitHub repo variables and the retired `asana_project_family_gid` /
  `calendar_family_id` lines from `terraform.tfvars`.
- [ ] **4.** Wait for the next scheduler tick (≤10 min) and confirm the move:
  `plan()` diffs against `due_day_events` rows keyed by `(day, calendar_id)`, so
  the affected days become a delete on primary and a create on "Ben | Cheryl"
  in one pass — not a duplicate. A day that also holds unrouted tasks keeps a
  primary event carrying only those. Check with the `fetch-tasks-logs` skill for
  the `POST /digest` outcome, and eyeball the two calendars.
- [ ] **5.** Run `scripts/fetch-env.sh` locally and confirm
  `.env` has a valid one-line `ASANA_PROJECT_CALENDARS='{...}'`, e.g.
  `(set -a; source .env; set +a; python -c "import json,os;print(json.loads(os.environ['ASANA_PROJECT_CALENDARS']))")`.

**Standing note:** after merge, keep `terraform/terraform.tfvars` and the
`ASANA_PROJECT_CALENDARS` GitHub repo variable in sync. A local
`/terraform-apply` run with a stale tfvars resets the variable to its `"{}"`
default and silently disables every project rule.

Nothing here spans a window that needs to revert as a unit: reverting the
merge alone fully reverts routing behavior, and the repo variable / tfvars
entries from step 1 are harmless leftovers until code merges again.

## Known gaps carried forward (spec → Open items)

- `repeat:` tags stay inert on both boards until
  `docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md` ships —
  accepted, not a coupling.
- The `cheryl` tag is still not written onto board members (D1); if UI
  legibility later proves it matters, the cheapest route is API-side
  enforcement in `api/routers/tasks.py` plus a sweep, and this routing rule
  stays regardless.
- Neither board's tasks reach the `tasks` DB or the `task_index` corpus; that
  follows from managing a project for webhooks, not from routing it (D5).

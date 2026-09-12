# Cross-project recurrence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a `repeat:` tag fire for a task in any configured Asana project and for a subtask, instead of silently doing nothing outside the single default project.

**Architecture:** An explicit `ASANA_MANAGED_PROJECTS` JSON map defines which projects the service acts on. A daily reconciler (`POST /webhook-sync`) registers one Asana webhook per managed project, each with its own `X-Hook-Secret` stored in a new `asana_webhooks` table and keyed by the `?project=` query parameter carried in the webhook target. Completion handling then resolves the Done section and the successor's placement from the completed task's own project (or its parent, for a subtask) rather than from `ASANA_PROJECT_ID`.

**Tech Stack:** Python 3.13, httpx, psycopg/pg8000 via `clients/db.py`, OpenTelemetry metrics, Terraform (Cloud Functions Gen2 + Cloud Scheduler), pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md` (extends `docs/superpowers/specs/2026-09-03-recurring-tasks-design.md`)

## Global Constraints

- **No personal identifiers in this repo.** Asana project gids and section gids live in `terraform.tfvars` and GitHub repo variables only. `terraform.tfvars.example` uses placeholders. Tests use fake gids like `p-ben`, `sec-done`.
- **No flag day (D7).** The webhook currently registered on Ben's Board has no `project` query parameter. Every change must leave it validating against the `ASANA_WEBHOOK_SECRET` env var, and reconciliation must never delete it.
- **Layer rules hold.** `repo/` takes an open connection and never opens its own; `services/` is pure logic with no HTTP; `handlers/` orchestrates; `main.py` is transport only.
- **DB best-effort, with one documented exception.** Webhook signature validation may reject on a cold-cache DB outage (D6) because Asana redelivers. Index refresh and digest flagging stay best-effort and must never fail a delivery.
- **Metrics are `asana.`-prefixed** OTel instruments declared in `clients/otel.py` (they export as `asana_*`).
- **Run tests with** `.venv/bin/pytest tests/ -q` from the repo root.
- **Branch off `main`**, never commit to it; open the PR with the `/pr-open` skill at the end.

## Decisions this plan makes beyond the spec

The spec leaves these open; they are settled here so tasks do not re-litigate them.

- **P1 — The managed map gets its own module.** `services/managed_projects.py` parses `ASANA_MANAGED_PROJECTS`; `services/sections.py` consumes it. One concern per file, matching `handlers/due_digest.py::_project_calendars`.
- **P2 — `asana.current_section` takes a project.** It is hardcoded to `ASANA_PROJECT_ID` today, so a task in another project would report no section and its successor would land unsectioned. A defaulted `project_gid` parameter fixes that without touching the two other callers.
- **P3 — A handshake that cannot store its secret fails closed (500).** Asana's create call then fails and no webhook exists whose deliveries could never be validated. This is what the spec's "no partial row" requires.
- **P4 — A managed project whose webhook exists but has no secret row is re-registered.** Delete then register, in that order. It is the only self-heal for a half-finished registration.
- **P5 — Reconciliation only ever touches webhooks whose target carries a `project` parameter.** That is what protects the legacy webhook (D7) from deletion.
- **P6 — The reconciler learns its own URL from the scheduler request.** Putting the function's own URI into its own environment would be a Terraform cycle, so the `tasks-webhook-sync` job passes `{"target": "<uri>"}` in the body, and the handler falls back to `request.url_root` for a hand-run curl.
- **P7 — `clients/asana.py::_request` gains a `timeout` parameter.** Webhook creation blocks on the synchronous handshake round-trip to a possibly cold CF; the 10-second default is too tight.

---

### Task 1: The managed-project map

**Files:**
- Create: `services/managed_projects.py`
- Test: `tests/test_managed_projects.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `managed() -> dict[str, dict]` — `{project_gid: {"done": str | None}}`
  - `gids() -> set[str]`
  - `done_section(project_gid: str | None) -> str | None`
  - `project_of(task: dict) -> str | None` — the task's own project gid, preferring a managed one
  - `ENV_VAR = "ASANA_MANAGED_PROJECTS"`

- [ ] **Step 1: Write the failing test**

Create `tests/test_managed_projects.py`:

```python
import json

from services import managed_projects as mp


def test_parses_the_map(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, json.dumps({"p1": {"done": "s1"}, "p2": {"done": None}}))
    assert mp.managed() == {"p1": {"done": "s1"}, "p2": {"done": None}}
    assert mp.gids() == {"p1", "p2"}
    assert mp.done_section("p1") == "s1"
    assert mp.done_section("p2") is None
    assert mp.done_section("p3") is None
    assert mp.done_section(None) is None


def test_unset_map_is_empty(monkeypatch):
    monkeypatch.delenv(mp.ENV_VAR, raising=False)
    assert mp.managed() == {}
    assert mp.gids() == set()


def test_malformed_map_degrades_to_empty(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, "{not json")
    assert mp.managed() == {}
    monkeypatch.setenv(mp.ENV_VAR, '["p1"]')
    assert mp.managed() == {}
    monkeypatch.setenv(mp.ENV_VAR, '{"p1": "s1"}')
    assert mp.managed() == {"p1": {"done": None}}


def test_project_of_prefers_a_managed_membership(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, json.dumps({"p2": {"done": "s2"}}))
    task = {
        "memberships": [
            {"project": {"gid": "p9"}},
            {"project": {"gid": "p2"}},
        ]
    }
    assert mp.project_of(task) == "p2"


def test_project_of_falls_back_to_the_first_membership(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of({"memberships": [{"project": {"gid": "p9"}}]}) == "p9"


def test_project_of_a_subtask_is_none(monkeypatch):
    monkeypatch.setenv(mp.ENV_VAR, "{}")
    assert mp.project_of({"memberships": []}) is None
    assert mp.project_of({}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_managed_projects.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.managed_projects'`

- [ ] **Step 3: Write the implementation**

Create `services/managed_projects.py`:

```python
"""The managed-project map: which Asana projects this service acts on.

ASANA_MANAGED_PROJECTS is a JSON object keyed by project gid:

    {"<gid>": {"done": "<done section gid>"}, "<other gid>": {"done": null}}

Membership *is* the definition of "managed": a webhook is registered for the
project, its completions are handled, and its Done move happens when `done`
is non-null. One place to add a project.

Project and section gids are personal — terraform.tfvars and the GitHub repo
variable only, never committed here.

Design: docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md (D2)
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

ENV_VAR = "ASANA_MANAGED_PROJECTS"


def managed() -> dict[str, dict]:
    """{project_gid: {"done": section gid | None}}.

    An unset or malformed map yields {}, which degrades the service to the
    single-default-project behavior it had before this feature rather than
    failing — same posture as the digest's project routing."""
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        logger.warning("%s is malformed (%s) — treating as empty", ENV_VAR, exc)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("%s is not a JSON object — treating as empty", ENV_VAR)
        return {}
    return {
        str(gid): {"done": (cfg.get("done") if isinstance(cfg, dict) else None) or None}
        for gid, cfg in parsed.items()
    }


def gids() -> set[str]:
    return set(managed())


def done_section(project_gid: str | None) -> str | None:
    """The configured Done section for a managed project, else None."""
    if not project_gid:
        return None
    return managed().get(project_gid, {}).get("done")


def project_of(task: dict) -> str | None:
    """The project a task lives in: its first managed membership, else its
    first membership at all, else None. A subtask has no memberships, so it
    yields None — callers treat that as "no project to act in"."""
    project_gids = [
        gid
        for m in task.get("memberships") or []
        if (gid := (m.get("project") or {}).get("gid"))
    ]
    known = managed()
    for gid in project_gids:
        if gid in known:
            return gid
    return project_gids[0] if project_gids else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_managed_projects.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add services/managed_projects.py tests/test_managed_projects.py
git commit -m "feat: parse the ASANA_MANAGED_PROJECTS map"
```

---

### Task 2: Project-aware Done section

**Files:**
- Modify: `services/sections.py:31-32` (the `done()` function)
- Modify: `handlers/task_complete.py:48` and the subtask guard above it
- Modify: `clients/asana.py:192-204` (`get_task` opt_fields — add `parent.gid`)
- Modify: `clients/asana.py:206-214` (`current_section` takes a project)
- Test: `tests/test_sections.py`, `tests/test_task_complete.py`, `tests/test_asana_client.py`

**Interfaces:**
- Consumes: `services.managed_projects.managed()` and `project_of` (Task 1).
- Produces:
  - `sections.done(project_gid: str | None = None) -> str | None`
  - `asana.current_section(task: dict, project_gid: str | None = None) -> dict | None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sections.py`:

```python
import json

from services import managed_projects


def test_done_uses_the_managed_project_section(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-default-done")
    monkeypatch.setenv(
        managed_projects.ENV_VAR,
        json.dumps({"p-family": {"done": "sec-family-done"}}),
    )
    assert sections.done("p-family") == "sec-family-done"


def test_done_falls_back_to_the_env_var_for_the_default_project(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-default-done")
    monkeypatch.setenv(managed_projects.ENV_VAR, "{}")
    assert sections.done("p-ben") == "sec-default-done"
    assert sections.done() == "sec-default-done"


def test_done_is_none_for_an_unmanaged_non_default_project(monkeypatch):
    monkeypatch.setenv("ASANA_PROJECT_ID", "p-ben")
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-default-done")
    monkeypatch.setenv(managed_projects.ENV_VAR, "{}")
    assert sections.done("p-stranger") is None


def test_done_is_none_for_a_managed_project_with_no_done_section(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-default-done")
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({"p-cheryl": {"done": None}}))
    assert sections.done("p-cheryl") is None
```

Append to `tests/test_asana_client.py`:

```python
def test_current_section_reads_the_named_project(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "p-ben")
    task = {
        "memberships": [
            {"project": {"gid": "p-ben"}, "section": {"gid": "s-ben", "name": "Review"}},
            {"project": {"gid": "p-family"}, "section": {"gid": "s-fam", "name": "Chores"}},
        ]
    }
    assert asana.current_section(task) == {"gid": "s-ben", "name": "Review"}
    assert asana.current_section(task, "p-family") == {"gid": "s-fam", "name": "Chores"}
    assert asana.current_section(task, "p-stranger") is None
```

Append to `tests/test_task_complete.py`:

```python
import json

from services import managed_projects


def test_completed_task_moves_to_its_own_projects_done(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-default-done")
    monkeypatch.setenv(
        managed_projects.ENV_VAR, json.dumps({"p-family": {"done": "sec-family-done"}})
    )
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        asana,
        "get_task",
        lambda gid: {
            "gid": gid,
            "completed": True,
            "memberships": [{"project": {"gid": "p-family"}}],
        },
    )
    monkeypatch.setattr(
        asana, "current_section", lambda task, project_gid=None: {"gid": "s1", "name": "Doing"}
    )
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_complete.handle("42")
    assert moves == [("42", "sec-family-done")]


def test_completed_subtask_is_never_moved(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-default-done")
    monkeypatch.setenv(managed_projects.ENV_VAR, "{}")
    monkeypatch.setattr(task_complete, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        asana,
        "get_task",
        lambda gid: {"gid": gid, "completed": True, "parent": {"gid": "p1"}, "memberships": []},
    )
    monkeypatch.setattr(
        asana, "current_section", lambda task, project_gid=None: None
    )
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_complete.handle("42")
    assert moves == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sections.py tests/test_task_complete.py tests/test_asana_client.py -q`
Expected: FAIL — `done()` takes no arguments, `current_section()` takes 1 positional
argument but 2 were given, and the subtask still moves to `sec-default-done`.

- [ ] **Step 3: Write the implementation**

In `services/sections.py`, add the import and replace `done()`:

```python
from services import managed_projects
```

```python
def done(project_gid: str | None = None) -> str | None:
    """The Done section for the project a completed task lives in (D8).

    - a managed project → its configured `done`, which may be None, meaning
      "this project has no Done section; skip the move"
    - an unmanaged project that is not the default one → None, same skip
    - the default project, or an unknown project (no membership information)
      → ASANA_SECTION_DONE_GID, preserving pre-D8 behavior exactly
    """
    known = managed_projects.managed()
    if project_gid and project_gid in known:
        return known[project_gid]["done"]
    if project_gid and project_gid != os.environ.get("ASANA_PROJECT_ID"):
        return None
    return os.environ.get("ASANA_SECTION_DONE_GID") or None
```

In `clients/asana.py::get_task`, add `parent.gid` to the opt_fields string:

```python
            "opt_fields": "completed,completed_at,name,parent.gid,tags.gid,tags.name,"
            "memberships.section.gid,memberships.section.name,memberships.project.gid"
```

Replace `clients/asana.py::current_section` so it can read a named project —
hardcoding `ASANA_PROJECT_ID` would report no section for a task anywhere else,
and both the Done-move comparison here and the successor's placement in Task 3
depend on it. The default keeps `handlers/label_applied.py` and
`services/escalation.py` working unchanged:

```python
def current_section(task: dict, project_gid: str | None = None) -> dict | None:
    """Return the task's {'gid', 'name'} section membership in the given
    project — the default project when none is named — or None."""
    target = project_gid or ASANA_PROJECT_ID
    for m in task.get("memberships", []):
        if (m.get("project") or {}).get("gid") == target:
            section = m.get("section") or {}
            if section.get("gid"):
                return {"gid": section["gid"], "name": section.get("name", "")}
    return None
```

In `handlers/task_complete.py`, import `managed_projects`, resolve the project once after the uncomplete check, and replace the Done-move block:

```python
from services import managed_projects, recurrence, sections
```

```python
    project_gid = managed_projects.project_of(task)
```

```python
    # A subtask has no project membership, so there is no Done section it
    # could belong to — moving it would add it to a project it is not in.
    if task.get("parent"):
        logger.info("Task %s is a subtask — completed, no Done move", task_gid)
        return

    done_gid = sections.done(project_gid)
    if not done_gid:
        logger.warning("No Done section for project %s — task %s left in place", project_gid, task_gid)
        return

    current = asana.current_section(task, project_gid)
```

- [ ] **Step 4: Run the full suite to verify nothing regressed**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS. Existing tests monkeypatch `current_section` with one-argument
lambdas; widen each to `lambda task, project_gid=None: ...` so the two-argument
call in `task_complete` reaches them.

- [ ] **Step 5: Commit**

```bash
git add services/sections.py handlers/task_complete.py clients/asana.py \
        tests/test_sections.py tests/test_task_complete.py tests/test_asana_client.py
git commit -m "feat: resolve the Done section from the task's own project"
```

---

### Task 3: Successor placement follows the source task

**Files:**
- Modify: `services/recurrence.py:128-176` (`spawn_next`)
- Modify: `handlers/task_complete.py:37` (pass the project through)
- Test: `tests/test_recurrence.py`

**Interfaces:**
- Consumes: `sections.done(project_gid)` and `asana.current_section(task, project_gid)` (Task 2), `managed_projects.project_of` (Task 1).
- Produces: `recurrence.spawn_next(task: dict, detail: dict, section: dict | None, rule: tuple[str, relativedelta], project_gid: str | None = None) -> str | None`

`clients/asana.py::DETAIL_OPT_FIELDS` already requests `parent.gid` and
`memberships.project.gid`, so `get_task_detail` needs no change — the spec's
"both are additive" note is already satisfied.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_recurrence.py`:

```python
def test_successor_lands_in_the_source_projects(monkeypatch):
    monkeypatch.setattr(asana, "find_task_by_external", lambda e: None)
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "p-ben")
    captured = {}

    def fake_create(fields):
        captured.update(fields)
        return SimpleNamespace(gid="new", permalink_url="https://asana/new")

    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)
    monkeypatch.setattr(asana, "remove_tag", lambda *a: None)
    monkeypatch.setattr(asana, "create_story", lambda *a, **k: None)
    monkeypatch.setattr(recurrence.task_index, "refresh", lambda gid: None)

    task = {"gid": "old", "name": "Renew", "completed_at": "2026-09-08T12:00:00.000Z"}
    detail = {
        "name": "Renew",
        "memberships": [{"project": {"gid": "p-family"}}, {"project": {"gid": "p-ben"}}],
    }
    recurrence.spawn_next(task, detail, None, ("tag1", relativedelta(months=3)), "p-family")
    assert captured["projects"] == ["p-family", "p-ben"]
    assert "parent" not in captured


def test_subtask_successor_goes_under_the_same_parent(monkeypatch):
    monkeypatch.setattr(asana, "find_task_by_external", lambda e: None)
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "p-ben")
    captured = {}
    sectioned = []

    def fake_create(fields):
        captured.update(fields)
        return SimpleNamespace(gid="new", permalink_url="https://asana/new")

    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)
    monkeypatch.setattr(asana, "add_task_to_section", lambda *a: sectioned.append(a))
    monkeypatch.setattr(asana, "remove_tag", lambda *a: None)
    monkeypatch.setattr(asana, "create_story", lambda *a, **k: None)
    monkeypatch.setattr(recurrence.task_index, "refresh", lambda gid: None)

    task = {"gid": "old", "name": "Water plants", "completed_at": "2026-09-08T12:00:00.000Z"}
    detail = {"name": "Water plants", "parent": {"gid": "parent-1"}, "memberships": []}
    recurrence.spawn_next(
        task, detail, {"gid": "s-any", "name": "Any"}, ("tag1", relativedelta(weeks=1)), None
    )
    assert captured["parent"] == "parent-1"
    assert "projects" not in captured
    assert sectioned == []
```

`SimpleNamespace` and `relativedelta` are already imported at the top of `tests/test_recurrence.py`; add `from types import SimpleNamespace` and `from dateutil.relativedelta import relativedelta` if the file does not already have them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_recurrence.py -q`
Expected: FAIL — `spawn_next() takes 4 positional arguments but 5 were given`, and once
that is past, `captured["projects"] == ["p-ben"]` instead of the source's own projects.

- [ ] **Step 3: Write the implementation**

In `services/recurrence.py::spawn_next`, change the signature and replace the placement block. Signature:

```python
def spawn_next(
    task: dict,
    detail: dict,
    section: dict | None,
    rule: tuple[str, relativedelta],
    project_gid: str | None = None,
) -> str | None:
```

Replace `if asana.ASANA_PROJECT_ID: fields["projects"] = [asana.ASANA_PROJECT_ID]` with:

```python
    # A subtask carries its parent instead of project membership; a top-level
    # task carries the source's own memberships, so a repeat: tag on Ben's
    # Board spawns onto Ben's Board rather than the service default (D9).
    parent_gid = (detail.get("parent") or task.get("parent") or {}).get("gid")
    if parent_gid:
        fields["parent"] = parent_gid
    else:
        source_projects = [
            gid
            for m in (detail.get("memberships") or task.get("memberships") or [])
            if (gid := (m.get("project") or {}).get("gid"))
        ]
        if source_projects:
            fields["projects"] = source_projects
        elif asana.ASANA_PROJECT_ID:
            fields["projects"] = [asana.ASANA_PROJECT_ID]
```

Replace the section-placement condition:

```python
    if not parent_gid and section and section["gid"] != sections.done(project_gid):
```

In `handlers/task_complete.py`, pass the project into both calls:

```python
            recurrence.spawn_next(
                task, detail, asana.current_section(task, project_gid), rule, project_gid
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/recurrence.py handlers/task_complete.py tests/test_recurrence.py
git commit -m "feat: place a successor in the source task's project or parent"
```

---

### Task 4: Asana webhook management calls

**Files:**
- Modify: `clients/asana.py:43-56` (`_request` timeout), and append `list_webhooks` / `create_webhook` / `delete_webhook` / `WEBHOOK_FILTERS`
- Modify: `scripts/register_webhook.py` (use the shared filter list)
- Test: `tests/test_asana_client.py`

**Interfaces:**
- Consumes: `_paginate`, `get_workspace_gid` (existing).
- Produces:
  - `asana.WEBHOOK_FILTERS: list[dict]`
  - `asana.list_webhooks() -> list[dict]` — each `{"gid", "target", "active", "resource": {"gid"}}`
  - `asana.create_webhook(resource_gid: str, target: str) -> dict` — `{"gid", "active", "target"}`
  - `asana.delete_webhook(webhook_gid: str) -> None`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_asana_client.py`:

```python
def test_create_webhook_posts_the_shared_filters(monkeypatch):
    sent = {}

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"gid": "w1", "active": True, "target": sent["json"]["data"]["target"]}}

    def fake_request(method, path, *, operation, timeout=10, **kwargs):
        sent.update({"method": method, "path": path, "timeout": timeout, **kwargs})
        return Resp()

    monkeypatch.setattr(asana, "_request", fake_request)
    hook = asana.create_webhook("p-family", "https://cf/?project=p-family")
    assert sent["method"] == "POST"
    assert sent["path"] == "/webhooks"
    assert sent["timeout"] > 10
    assert sent["json"]["data"]["resource"] == "p-family"
    assert sent["json"]["data"]["filters"] == asana.WEBHOOK_FILTERS
    assert hook["gid"] == "w1"


def test_delete_webhook_tolerates_a_missing_webhook(monkeypatch):
    class Resp:
        status_code = 404

        def raise_for_status(self):
            raise AssertionError("must not raise on 404")

    monkeypatch.setattr(asana, "_request", lambda *a, **k: Resp())
    asana.delete_webhook("gone")


def test_list_webhooks_paginates_by_workspace(monkeypatch):
    monkeypatch.setattr(asana, "get_workspace_gid", lambda: "ws1")
    captured = {}

    def fake_paginate(path, params, *, operation):
        captured.update({"path": path, "params": params, "operation": operation})
        return [{"gid": "w1", "target": "https://cf/?project=p1"}]

    monkeypatch.setattr(asana, "_paginate", fake_paginate)
    assert asana.list_webhooks() == [{"gid": "w1", "target": "https://cf/?project=p1"}]
    assert captured["params"]["workspace"] == "ws1"
    assert "target" in captured["params"]["opt_fields"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_asana_client.py -q`
Expected: FAIL — `AttributeError: module 'clients.asana' has no attribute 'create_webhook'`

- [ ] **Step 3: Write the implementation**

In `clients/asana.py`, widen `_request`:

```python
def _request(method: str, path: str, *, operation: str, timeout: float = 10, **kwargs) -> httpx.Response:
    """Single choke point for Asana calls — records asana.api.duration per operation."""
    t0 = time.monotonic()
    try:
        return httpx.request(
            method,
            f"{_BASE}{path}",
            headers={"Authorization": f"Bearer {ASANA_API_KEY}"},
            timeout=timeout,
            **kwargs,
        )
    finally:
        otel.api_duration.record((time.monotonic() - t0) * 1000, {"operation": operation})
```

Append the webhook calls:

```python
# Registered filters are the delivery gate: an event type missing here never
# reaches the CF, no matter what handlers/asana_webhook.py::receive supports.
# Keep in sync with that function.
WEBHOOK_FILTERS = [
    {
        "resource_type": "task",
        "action": "changed",
        "fields": ["completed", "name", "notes", "due_on"],
    },
    {"resource_type": "task", "action": "added"},
    {"resource_type": "task", "action": "deleted"},
    {"resource_type": "task", "action": "removed"},
]


def list_webhooks() -> list[dict]:
    """Every webhook in the workspace: [{gid, target, active, resource}]."""
    return _paginate(
        "/webhooks",
        {"workspace": get_workspace_gid(), "opt_fields": "target,active,resource.gid"},
        operation="list_webhooks",
    )


def create_webhook(resource_gid: str, target: str) -> dict:
    """Register a webhook and return {gid, active, target}.

    Asana calls `target` with X-Hook-Secret and waits for the echo before
    this POST returns, so the round trip includes a possibly cold CF start —
    hence the long timeout."""
    resp = _request(
        "POST",
        "/webhooks",
        operation="create_webhook",
        json={"data": {"resource": resource_gid, "target": target, "filters": WEBHOOK_FILTERS}},
        params={"opt_fields": "gid,active,target"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def delete_webhook(webhook_gid: str) -> None:
    """Delete a webhook. A 404 is success — Asana already removed it."""
    resp = _request("DELETE", f"/webhooks/{webhook_gid}", operation="delete_webhook")
    if resp.status_code == 404:
        return
    resp.raise_for_status()
```

In `scripts/register_webhook.py`, replace the inline `"filters": [...]` literal with the shared list so the two paths cannot drift:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from clients.asana import WEBHOOK_FILTERS
```

```python
                "filters": WEBHOOK_FILTERS,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_asana_client.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clients/asana.py scripts/register_webhook.py tests/test_asana_client.py
git commit -m "feat: list, create and delete Asana webhooks"
```

---

### Task 5: The `asana_webhooks` table

**Files:**
- Create: `repo/asana_webhooks.py`
- Modify: `repo/schema.sql` (append the table)
- Test: `tests/test_repo_asana_webhooks.py`

**Interfaces:**
- Consumes: an open connection (never opens its own — layer rule).
- Produces:
  - `upsert_secret(conn, project_gid: str, secret: str) -> None`
  - `set_webhook_gid(conn, project_gid: str, webhook_gid: str) -> None`
  - `get_secret(conn, project_gid: str) -> str | None`
  - `list_all(conn) -> list[dict]` — rows of `{project_gid, webhook_gid, secret}`
  - `delete(conn, project_gid: str) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_asana_webhooks.py`:

```python
from repo import asana_webhooks as repo
from tests.test_repo import FakeConn
from tests.test_repo_due_digest import RowsConn


def test_upsert_replaces_the_secret_and_clears_the_webhook_gid():
    conn = FakeConn()
    repo.upsert_secret(conn, "p1", "shh")
    query, params = conn.executed[0]
    assert "INSERT INTO asana_webhooks" in query
    assert "ON CONFLICT (project_gid) DO UPDATE" in query
    assert "webhook_gid = NULL" in query
    assert params == ("p1", "shh")


def test_set_webhook_gid_updates_by_project():
    conn = FakeConn()
    repo.set_webhook_gid(conn, "p1", "w1")
    query, params = conn.executed[0]
    assert "UPDATE asana_webhooks" in query
    assert params == ("w1", "p1")


def test_get_secret_returns_none_when_absent():
    assert repo.get_secret(FakeConn(row=None), "p1") is None
    assert repo.get_secret(FakeConn(row={"secret": "shh"}), "p1") == "shh"


def test_list_all_returns_every_row():
    conn = RowsConn(rows=[{"project_gid": "p1", "webhook_gid": "w1", "secret": "shh"}])
    assert repo.list_all(conn) == [{"project_gid": "p1", "webhook_gid": "w1", "secret": "shh"}]


def test_delete_removes_by_project():
    conn = FakeConn()
    repo.delete(conn, "p1")
    query, params = conn.executed[0]
    assert "DELETE FROM asana_webhooks" in query
    assert params == ("p1",)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_repo_asana_webhooks.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'repo.asana_webhooks'`

- [ ] **Step 3: Write the implementation**

Create `repo/asana_webhooks.py`:

```python
"""Per-project Asana webhook secrets.

Asana mints one X-Hook-Secret per webhook and it cannot be supplied by the
caller, so N managed projects means N secrets. They live here rather than in
Secret Manager because the webhook CF is publicly invokable by necessity and
must not hold secretmanager.versions.add (D3).

The row is written at handshake time, when the webhook gid does not exist yet
— the project gid from the target's query string is the only key available
(D4). The reconciler fills webhook_gid in afterwards.
"""

from typing import Any


def upsert_secret(conn: Any, project_gid: str, secret: str) -> None:
    """Store the handshake secret for a project, replacing any previous one.

    webhook_gid is cleared: a new handshake means a new webhook, and the
    reconciler has not yet learned its gid."""
    conn.execute(
        """
        INSERT INTO asana_webhooks (project_gid, secret, registered_at)
        VALUES (%s, %s, now())
        ON CONFLICT (project_gid) DO UPDATE
            SET secret = EXCLUDED.secret,
                webhook_gid = NULL,
                registered_at = now()
        """,
        (project_gid, secret),
    )


def set_webhook_gid(conn: Any, project_gid: str, webhook_gid: str) -> None:
    conn.execute(
        "UPDATE asana_webhooks SET webhook_gid = %s WHERE project_gid = %s",
        (webhook_gid, project_gid),
    )


def get_secret(conn: Any, project_gid: str) -> str | None:
    row = conn.execute(
        "SELECT secret FROM asana_webhooks WHERE project_gid = %s",
        (project_gid,),
    ).fetchone()
    return row["secret"] if row else None


def list_all(conn: Any) -> list[dict]:
    return conn.execute(
        "SELECT project_gid, webhook_gid, secret FROM asana_webhooks ORDER BY project_gid"
    ).fetchall()


def delete(conn: Any, project_gid: str) -> None:
    conn.execute("DELETE FROM asana_webhooks WHERE project_gid = %s", (project_gid,))
```

Append to `repo/schema.sql`:

```sql
-- One Asana webhook per managed project, each with its own X-Hook-Secret
-- (docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md, D3).
-- Written at handshake time keyed on project_gid, because the webhook gid
-- does not exist until the registering POST returns.
CREATE TABLE IF NOT EXISTS asana_webhooks (
    project_gid   TEXT PRIMARY KEY,
    webhook_gid   TEXT,
    secret        TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_repo_asana_webhooks.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add repo/asana_webhooks.py repo/schema.sql tests/test_repo_asana_webhooks.py
git commit -m "feat: store per-project Asana webhook secrets"
```

---

### Task 6: The reconciliation diff

**Files:**
- Create: `services/webhook_registry.py`
- Test: `tests/test_webhook_registry.py`

**Interfaces:**
- Consumes: nothing — pure functions over sets and dicts, no Asana, no DB.
- Produces:
  - `Plan = NamedTuple("Plan", to_register: list[str], to_delete: list[tuple[str, str]])`
  - `target_project(target: str, base_url: str) -> str | None`
  - `plan(managed: set[str], registered: dict[str, str], with_secrets: set[str]) -> Plan`

- [ ] **Step 1: Write the failing test**

Create `tests/test_webhook_registry.py`:

```python
from services import webhook_registry as reg

BASE = "https://us-central1-x.cloudfunctions.net/tasks-webhook"


def test_target_project_reads_the_query_parameter():
    assert reg.target_project(f"{BASE}?project=p1", BASE) == "p1"
    assert reg.target_project(f"{BASE}/?project=p1", BASE) == "p1"


def test_a_target_without_a_project_is_left_alone():
    """The legacy single-project webhook (D7) must survive reconciliation."""
    assert reg.target_project(BASE, BASE) is None


def test_someone_elses_webhook_is_left_alone():
    assert reg.target_project("https://example.com/hook?project=p1", BASE) is None
    assert reg.target_project("", BASE) is None


def test_empty_state_registers_everything():
    p = reg.plan({"p1", "p2"}, {}, set())
    assert p.to_register == ["p1", "p2"]
    assert p.to_delete == []


def test_steady_state_does_nothing():
    p = reg.plan({"p1"}, {"p1": "w1"}, {"p1"})
    assert p == reg.Plan([], [])


def test_a_new_project_is_registered():
    p = reg.plan({"p1", "p2"}, {"p1": "w1"}, {"p1"})
    assert p.to_register == ["p2"]
    assert p.to_delete == []


def test_an_unmanaged_project_is_deregistered():
    p = reg.plan({"p1"}, {"p1": "w1", "p9": "w9"}, {"p1", "p9"})
    assert p.to_register == []
    assert p.to_delete == [("p9", "w9")]


def test_a_webhook_with_no_secret_row_is_replaced():
    p = reg.plan({"p1"}, {"p1": "w1"}, set())
    assert p.to_delete == [("p1", "w1")]
    assert p.to_register == ["p1"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_webhook_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'services.webhook_registry'`

- [ ] **Step 3: Write the implementation**

Create `services/webhook_registry.py`:

```python
"""Pure reconciliation diff: managed projects vs. registered webhooks.

No Asana, no database — handlers/webhook_sync.py does the I/O against this.

Design: docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md (D5)
"""

from typing import NamedTuple
from urllib.parse import parse_qs, urlparse


class Plan(NamedTuple):
    to_register: list[str]  # project gids
    to_delete: list[tuple[str, str]]  # (project gid, webhook gid)


def target_project(target: str, base_url: str) -> str | None:
    """The `project` query parameter of one of our webhook targets.

    None for a target that is not ours, or that carries no project — which is
    exactly the legacy single-project webhook (D7). Returning None there is
    what keeps reconciliation from deleting it during the rollout."""
    if not target:
        return None
    parsed, base = urlparse(target), urlparse(base_url)
    if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
        return None
    return (parse_qs(parsed.query).get("project") or [None])[0]


def plan(managed: set[str], registered: dict[str, str], with_secrets: set[str]) -> Plan:
    """What to change so every managed project has exactly one live webhook
    whose secret we hold.

    `registered` is {project gid: webhook gid} for our own project-scoped
    webhooks; `with_secrets` is the set of projects with an asana_webhooks row.

    Deletes must be applied before registrations: a project whose webhook has
    no secret row appears in both lists, and the replacement only works in
    that order."""
    to_register: list[str] = []
    to_delete: list[tuple[str, str]] = []

    for gid in sorted(managed):
        webhook_gid = registered.get(gid)
        if webhook_gid is None:
            to_register.append(gid)
        elif gid not in with_secrets:
            # Half-finished registration: the webhook exists but we cannot
            # validate anything it delivers. Replace it rather than leave a
            # project silently dead — the failure this whole spec is about.
            to_delete.append((gid, webhook_gid))
            to_register.append(gid)

    for gid, webhook_gid in sorted(registered.items()):
        if gid not in managed:
            to_delete.append((gid, webhook_gid))

    return Plan(to_register, to_delete)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_webhook_registry.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add services/webhook_registry.py tests/test_webhook_registry.py
git commit -m "feat: diff managed projects against registered webhooks"
```

---

### Task 7: Per-project handshake and signature validation

**Files:**
- Modify: `handlers/asana_webhook.py:22-38` (`handshake`, `signature_valid`) and `receive`
- Modify: `clients/otel.py` (add `webhook_auth_failures`)
- Modify: `main.py:79-82` and `main.py:104-106` (pass the `project` query parameter)
- Test: `tests/test_asana_webhook.py`, `tests/test_main.py`

**Interfaces:**
- Consumes: `repo.asana_webhooks.get_secret` / `upsert_secret` (Task 5), `managed_projects.gids` (Task 1).
- Produces:
  - `asana_webhook.handshake(hook_secret: str, project_gid: str | None = None) -> tuple`
  - `asana_webhook.signature_valid(body: bytes, signature: str, project_gid: str | None = None) -> bool`
  - `asana_webhook.receive(body: bytes, signature: str, project_gid: str | None = None) -> tuple`
  - `asana_webhook._secret_cache: dict[str, tuple[float, str]]` and `_SECRET_TTL_SECONDS = 600`
  - `otel.webhook_auth_failures: metrics.Counter`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_asana_webhook.py` (and add `from tests.test_repo import FakeConn` at the top):

```python
import pytest

PROJECT_SECRET = "per-project"


@pytest.fixture(autouse=True)
def _clear_secret_cache():
    asana_webhook._secret_cache.clear()
    yield
    asana_webhook._secret_cache.clear()


def _signed_with(secret, events):
    body = json.dumps({"events": events}).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, sig


def test_per_project_secret_validates(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(
        asana_webhook, "get_conn", lambda: FakeConn(row={"secret": PROJECT_SECRET})
    )
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-family") == ("", 200)


def test_another_projects_secret_is_rejected(monkeypatch):
    _capture(monkeypatch)
    monkeypatch.setattr(
        asana_webhook, "get_conn", lambda: FakeConn(row={"secret": PROJECT_SECRET})
    )
    body, sig = _signed_with(
        "wrong-secret", [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-family") == ("", 401)


def test_no_project_parameter_falls_back_to_the_env_secret(monkeypatch):
    """D7: the legacy webhook keeps working through the rollout."""
    _capture(monkeypatch)
    body, sig = _signed([{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}])
    assert asana_webhook.receive(body, sig) == ("", 200)


def test_a_cached_secret_avoids_the_database(monkeypatch):
    _capture(monkeypatch)
    reads = []

    def counting_conn():
        reads.append(1)
        return FakeConn(row={"secret": PROJECT_SECRET})

    monkeypatch.setattr(asana_webhook, "get_conn", counting_conn)
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    asana_webhook.receive(body, sig, "p-family")
    asana_webhook.receive(body, sig, "p-family")
    assert len(reads) == 1


def test_an_expired_cache_entry_is_re_read(monkeypatch):
    _capture(monkeypatch)
    reads = []

    def counting_conn():
        reads.append(1)
        return FakeConn(row={"secret": PROJECT_SECRET})

    monkeypatch.setattr(asana_webhook, "get_conn", counting_conn)
    clock = [1000.0]
    monkeypatch.setattr(asana_webhook, "_now", lambda: clock[0])
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    asana_webhook.receive(body, sig, "p-family")
    clock[0] += asana_webhook._SECRET_TTL_SECONDS + 1
    asana_webhook.receive(body, sig, "p-family")
    assert len(reads) == 2


def test_db_outage_with_a_cold_cache_rejects_rather_than_raises(monkeypatch):
    _capture(monkeypatch)

    def boom():
        raise RuntimeError("cloud sql unreachable")

    monkeypatch.setattr(asana_webhook, "get_conn", boom)
    body, sig = _signed_with(
        PROJECT_SECRET, [{"action": "added", "resource": {"gid": "t1", "resource_type": "task"}}]
    )
    assert asana_webhook.receive(body, sig, "p-family") == ("", 401)


def test_handshake_stores_the_secret_for_a_project(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(asana_webhook, "get_conn", lambda: conn)
    body, status, headers = asana_webhook.handshake("shh", "p-family")
    assert status == 200
    assert headers["X-Hook-Secret"] == "shh"
    assert any("INSERT INTO asana_webhooks" in q for q, _ in conn.executed)


def test_handshake_without_a_project_still_echoes(monkeypatch):
    body, status, headers = asana_webhook.handshake("shh")
    assert (status, headers["X-Hook-Secret"]) == (200, "shh")


def test_handshake_fails_closed_when_the_secret_cannot_be_stored(monkeypatch):
    def boom():
        raise RuntimeError("cloud sql unreachable")

    monkeypatch.setattr(asana_webhook, "get_conn", boom)
    assert asana_webhook.handshake("shh", "p-family") == ("", 500)
```

Append to `tests/test_main.py`:

```python
def test_webhook_passes_the_project_query_parameter(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        main.asana_webhook,
        "receive",
        lambda body, sig, project=None: seen.update({"project": project}) or ("", 200),
    )
    request = _FakeRequest(path="/", method="POST", args={"project": "p-family"})
    main.webhook(request)
    assert seen["project"] == "p-family"
```

Use whatever fake-request helper `tests/test_main.py` already defines; if it has none, add one exposing `path`, `method`, `headers`, `args`, and `get_data()`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_asana_webhook.py tests/test_main.py -q`
Expected: FAIL — `receive() takes 2 positional arguments but 3 were given`, and `_secret_cache` does not exist.

- [ ] **Step 3: Write the implementation**

In `clients/otel.py`, add the no-op declaration next to the others:

```python
webhook_auth_failures: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
```

add it to the `global` statement in `setup_telemetry`, and create it alongside the other instruments:

```python
    webhook_auth_failures = meter.create_counter(
        "asana.webhook.auth_failures",
        description="Rejected webhook deliveries by reason "
        "(unknown_project|no_secret|bad_signature)",
    )
```

In `handlers/asana_webhook.py`, add imports and the cache:

```python
import time

import clients.otel as otel
from repo import asana_webhooks as repo_webhooks
from services import managed_projects, task_index
```

```python
# Steady state does no database read on the delivery path. Correctness never
# depends on the cache: a miss falls through to Postgres, and a miss during a
# DB outage rejects, which Asana retries (D6).
_SECRET_TTL_SECONDS = 600
_secret_cache: dict[str, tuple[float, str]] = {}


def _now() -> float:
    return time.monotonic()


def _secret_for(project_gid: str) -> str | None:
    cached = _secret_cache.get(project_gid)
    if cached and _now() - cached[0] < _SECRET_TTL_SECONDS:
        return cached[1]
    try:
        with get_conn() as conn:
            secret = repo_webhooks.get_secret(conn, project_gid)
    except Exception:
        # The one place a DB outage may cost a delivery. Safe only because
        # rejecting is non-destructive here — Asana redelivers for 24 hours.
        logger.warning(
            "Webhook secret lookup failed for project %s — rejecting, Asana will retry",
            project_gid,
            exc_info=True,
        )
        return None
    if secret:
        _secret_cache[project_gid] = (_now(), secret)
    return secret
```

Replace `handshake` and `signature_valid`:

```python
def handshake(hook_secret: str, project_gid: str | None = None) -> tuple:
    """Echo X-Hook-Secret, storing it against the project that is registering.

    Without a project this is the legacy single-webhook path (D7): the secret
    is logged so the runbook can put it in Secret Manager by hand."""
    if not project_gid:
        logger.info("Asana webhook handshake — X-Hook-Secret: %s", hook_secret)
        return "", 200, {"X-Hook-Secret": hook_secret}
    try:
        with get_conn() as conn:
            repo_webhooks.upsert_secret(conn, project_gid, hook_secret)
    except Exception:
        # Fail the handshake rather than echo: Asana's create call then fails
        # and no webhook exists whose deliveries we could never validate.
        logger.exception("Webhook handshake: storing the secret for %s failed", project_gid)
        return "", 500
    _secret_cache[project_gid] = (_now(), hook_secret)
    logger.info("Asana webhook handshake stored for project %s", project_gid)
    return "", 200, {"X-Hook-Secret": hook_secret}


def signature_valid(body: bytes, signature: str, project_gid: str | None = None) -> bool:
    if project_gid:
        secret = _secret_for(project_gid)
        if not secret:
            reason = "no_secret" if project_gid in managed_projects.gids() else "unknown_project"
            otel.webhook_auth_failures.add(1, {"reason": reason})
            logger.warning("No webhook secret for project %s (%s)", project_gid, reason)
            return False
    else:
        secret = os.environ.get("ASANA_WEBHOOK_SECRET", "")
        if not secret:
            otel.webhook_auth_failures.add(1, {"reason": "no_secret"})
            logger.warning("ASANA_WEBHOOK_SECRET not set — rejecting webhook event")
            return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        otel.webhook_auth_failures.add(1, {"reason": "bad_signature"})
        return False
    return True
```

Change `receive`'s signature and its first line:

```python
def receive(body: bytes, signature: str, project_gid: str | None = None) -> tuple:
    """Validate and dispatch one webhook delivery.

    `project_gid` comes from the target's ?project= parameter and selects the
    secret; dispatch itself is project-agnostic, because each task carries its
    own memberships."""
    if not signature_valid(body, signature, project_gid):
        logger.warning("Invalid webhook signature — rejecting")
        return "", 401
```

In `main.py`, pass the query parameter in both places:

```python
        hook_secret = request.headers.get("X-Hook-Secret")
        if hook_secret:
            return asana_webhook.handshake(hook_secret, request.args.get("project"))
```

```python
        return asana_webhook.receive(
            request.get_data(),
            request.headers.get("X-Hook-Signature", ""),
            request.args.get("project"),
        )
```

- [ ] **Step 4: Run the full suite to verify it passes**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS — including the pre-existing `tests/test_asana_webhook.py` cases, which call `receive(body, sig)` and take the env-secret path.

- [ ] **Step 5: Commit**

```bash
git add handlers/asana_webhook.py clients/otel.py main.py \
        tests/test_asana_webhook.py tests/test_main.py
git commit -m "feat: validate webhook deliveries against a per-project secret"
```

---

### Task 8: The reconciler

**Files:**
- Create: `handlers/webhook_sync.py`
- Modify: `clients/otel.py` (add `webhooks_registered`, `webhooks_deleted`, `webhooks_active`)
- Modify: `main.py` (route `POST /webhook-sync`)
- Test: `tests/test_webhook_sync.py`

**Interfaces:**
- Consumes: `asana.list_webhooks/create_webhook/delete_webhook` (Task 4), `repo.asana_webhooks` (Task 5), `webhook_registry.plan/target_project` (Task 6), `managed_projects.gids` (Task 1).
- Produces:
  - `webhook_sync.run(target_url: str) -> dict` — `{"managed": int, "registered": int, "deleted": int, "active": int}`
  - `otel.webhooks_registered` / `otel.webhooks_deleted` (Counters), `otel.webhooks_active` (`metrics._Gauge`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_webhook_sync.py`:

```python
import json

import clients.asana as asana
from handlers import webhook_sync
from services import managed_projects
from tests.test_repo import FakeConn
from tests.test_repo_due_digest import RowsConn

BASE = "https://cf.example/tasks-webhook"


def _managed(monkeypatch, *gids):
    monkeypatch.setenv(managed_projects.ENV_VAR, json.dumps({g: {"done": None} for g in gids}))


def test_registers_a_missing_project(monkeypatch):
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [])
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))
    created = []
    monkeypatch.setattr(
        asana,
        "create_webhook",
        lambda resource, target: created.append((resource, target)) or {"gid": "w1"},
    )
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: None)

    result = webhook_sync.run(BASE)
    assert created == [("p1", f"{BASE}?project=p1")]
    assert result == {"managed": 1, "registered": 1, "deleted": 0, "active": 1}


def test_leaves_the_legacy_webhook_alone(monkeypatch):
    """A webhook whose target carries no ?project= is the pre-rollout one (D7)."""
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [{"gid": "legacy", "target": BASE}])
    monkeypatch.setattr(
        webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}])
    )
    deleted = []
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: deleted.append(gid))
    monkeypatch.setattr(asana, "create_webhook", lambda resource, target: {"gid": "w1"})

    webhook_sync.run(BASE)
    assert deleted == []


def test_deregisters_a_project_that_left_the_map(monkeypatch):
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana,
        "list_webhooks",
        lambda: [
            {"gid": "w1", "target": f"{BASE}?project=p1"},
            {"gid": "w9", "target": f"{BASE}?project=p9"},
        ],
    )
    monkeypatch.setattr(
        webhook_sync,
        "get_conn",
        lambda: RowsConn(rows=[{"project_gid": "p1"}, {"project_gid": "p9"}]),
    )
    deleted = []
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: deleted.append(gid))
    monkeypatch.setattr(asana, "create_webhook", lambda resource, target: {"gid": "new"})

    result = webhook_sync.run(BASE)
    assert deleted == ["w9"]
    assert result["deleted"] == 1
    assert result["active"] == 1


def test_steady_state_changes_nothing(monkeypatch):
    _managed(monkeypatch, "p1")
    monkeypatch.setattr(
        asana, "list_webhooks", lambda: [{"gid": "w1", "target": f"{BASE}?project=p1"}]
    )
    monkeypatch.setattr(
        webhook_sync, "get_conn", lambda: RowsConn(rows=[{"project_gid": "p1"}])
    )
    monkeypatch.setattr(
        asana, "create_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no create"))
    )
    monkeypatch.setattr(
        asana, "delete_webhook", lambda *a: (_ for _ in ()).throw(AssertionError("no delete"))
    )

    assert webhook_sync.run(BASE) == {
        "managed": 1,
        "registered": 0,
        "deleted": 0,
        "active": 1,
    }


def test_a_failed_registration_does_not_stop_the_others(monkeypatch):
    _managed(monkeypatch, "p1", "p2")
    monkeypatch.setattr(asana, "list_webhooks", lambda: [])
    monkeypatch.setattr(webhook_sync, "get_conn", lambda: RowsConn(rows=[]))

    def flaky(resource, target):
        if resource == "p1":
            raise RuntimeError("asana 500")
        return {"gid": "w2"}

    monkeypatch.setattr(asana, "create_webhook", flaky)
    monkeypatch.setattr(asana, "delete_webhook", lambda gid: None)

    result = webhook_sync.run(BASE)
    assert result["registered"] == 1
    assert result["active"] == 1
```

`RowsConn` is reused for `list_all`; a `FakeConn` import stays available for the write paths if a test needs to assert on them.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_webhook_sync.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'handlers.webhook_sync'`

- [ ] **Step 3: Write the implementation**

In `clients/otel.py`, add the declarations:

```python
webhooks_registered: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
webhooks_deleted: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
webhooks_active: metrics._Gauge = metrics.NoOpMeter("noop").create_gauge("noop")
```

add all three to the `global` statement in `setup_telemetry`, and create them:

```python
    webhooks_registered = meter.create_counter(
        "asana.webhooks.registered", description="Project webhooks registered by the reconciler"
    )
    webhooks_deleted = meter.create_counter(
        "asana.webhooks.deleted", description="Project webhooks deleted by the reconciler"
    )
    webhooks_active = meter.create_gauge(
        "asana.webhooks.active",
        description="Managed projects with a live webhook — below the managed count means "
        "deliveries are being dropped",
    )
```

Create `handlers/webhook_sync.py`:

```python
"""Reconcile Asana webhook registrations against the managed-project map.

Asana deletes a webhook after 24 hours of failed delivery and mints a fresh
X-Hook-Secret for every new one, so registration has to be a repairable
steady state rather than a runbook step. Cloud Scheduler drives this daily
(tasks-webhook-sync → POST /webhook-sync).

Unlike the delivery path, this handler lets a database failure raise: a run
that cannot read its own secret rows would compute a diff that deletes and
re-registers everything. Failing is correct here — the next tick retries.

Design: docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md (D5)
"""

import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import asana_webhooks as repo_webhooks
from services import managed_projects, webhook_registry

logger = logging.getLogger(__name__)


def run(target_url: str) -> dict:
    """Bring registrations in line with ASANA_MANAGED_PROJECTS."""
    managed = managed_projects.gids()

    registered: dict[str, str] = {}
    for hook in asana.list_webhooks():
        project_gid = webhook_registry.target_project(hook.get("target") or "", target_url)
        if project_gid:
            registered[project_gid] = hook["gid"]

    with get_conn() as conn:
        with_secrets = {row["project_gid"] for row in repo_webhooks.list_all(conn)}

    plan = webhook_registry.plan(managed, registered, with_secrets)
    live = {gid for gid in registered if gid in managed}

    # Deletes first: a project being replaced (webhook with no secret row)
    # appears in both lists and only reconciles in that order.
    deleted = 0
    for project_gid, webhook_gid in plan.to_delete:
        try:
            asana.delete_webhook(webhook_gid)
        except Exception:
            logger.exception(
                "Webhook sync: deleting %s for project %s failed", webhook_gid, project_gid
            )
            continue
        deleted += 1
        live.discard(project_gid)
        if project_gid not in managed:
            with get_conn() as conn:
                repo_webhooks.delete(conn, project_gid)

    registered_count = 0
    for project_gid in plan.to_register:
        try:
            # Asana calls back into handshake() during this POST; that is what
            # writes the secret row keyed on the ?project= parameter (D4).
            hook = asana.create_webhook(project_gid, f"{target_url}?project={project_gid}")
            with get_conn() as conn:
                repo_webhooks.set_webhook_gid(conn, project_gid, hook["gid"])
        except Exception:
            logger.exception(
                "Webhook sync: registering project %s failed — retrying next tick", project_gid
            )
            continue
        registered_count += 1
        live.add(project_gid)

    otel.webhooks_registered.add(registered_count)
    otel.webhooks_deleted.add(deleted)
    otel.webhooks_active.set(len(live))
    logger.info(
        "Webhook sync: %d managed, %d registered, %d deleted, %d active",
        len(managed),
        registered_count,
        deleted,
        len(live),
    )
    return {
        "managed": len(managed),
        "registered": registered_count,
        "deleted": deleted,
        "active": len(live),
    }
```

In `main.py`, import the handler and add the route above the `request.method != "POST"` guard:

```python
from handlers import asana_webhook, due_digest, label_applied, task_create, webhook_sync
```

```python
        if request.path == "/webhook-sync" and request.method == "POST":
            if not escalation.is_authorized(request.headers.get("Authorization")):
                return "", 401
            try:
                body = json.loads(request.get_data() or b"{}")
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            # The scheduler passes the function's own URI; putting it in this
            # function's own env would be a Terraform cycle. A hand-run curl
            # gets it from the request instead.
            target = str(body.get("target") or "").strip() or request.url_root.rstrip("/")
            return webhook_sync.run(target), 200
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add handlers/webhook_sync.py clients/otel.py main.py tests/test_webhook_sync.py
git commit -m "feat: reconcile project webhooks on a schedule"
```

---

### Task 9: Configuration, scheduling, and docs

**Files:**
- Modify: `terraform/variables.tf` (add `asana_managed_projects`)
- Modify: `terraform/cloud_functions.tf:5-23` (`common_env`)
- Modify: `terraform/scheduler.tf` (add `tasks-webhook-sync`)
- Modify: `terraform/terraform.tfvars.example`
- Modify: `.github/workflows/deploy.yml:43-58` (add the TF_VAR)
- Modify: `main.py` module docstring (env var list, `/webhook-sync` route)
- Modify: `docs/asana-webhook-setup.md`, `CLAUDE.md`
- Test: none — configuration and prose. Verified by `terraform validate` and the plan in Task 10.

**Interfaces:**
- Consumes: `ASANA_MANAGED_PROJECTS` (Task 1), `POST /webhook-sync` (Task 8).
- Produces: the env var and Cloud Scheduler job the rollout needs.

- [ ] **Step 1: Add the Terraform variable**

In `terraform/variables.tf`, next to `asana_project_calendars`:

```hcl
variable "asana_managed_projects" {
  description = "Projects this service manages, as a JSON object: {\"<project gid>\": {\"done\": \"<done section gid>\"}}. Membership means a webhook is registered for the project and its completions are handled; \"done\": null means the project has no Done section and completed tasks are left in place. Personal gids: terraform.tfvars + GitHub repo variable only. \"{}\" leaves only the legacy single-project webhook in play."
  type        = string
  default     = "{}"
}
```

- [ ] **Step 2: Wire the env var and the scheduler job**

In `terraform/cloud_functions.tf`, add to `local.common_env`:

```hcl
    ASANA_MANAGED_PROJECTS    = var.asana_managed_projects
```

Append to `terraform/scheduler.tf`:

```hcl
# ---------------------------------------------------------------------------
# Webhook reconciliation — daily. Asana deletes a webhook after 24 hours of
# failed delivery, so this is what makes a dropped registration self-healing
# rather than a silent end to recurrence in that project.
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "webhook_sync" {
  name      = "tasks-webhook-sync"
  schedule  = "30 5 * * *"
  time_zone = "America/New_York"

  # Registration blocks on Asana's synchronous handshake round-trip per
  # project; five projects fit comfortably, a retry buys nothing before the
  # next daily tick.
  attempt_deadline = "300s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloudfunctions2_function.tasks_webhook.service_config[0].uri}/webhook-sync"
    body = base64encode(jsonencode({
      target = google_cloudfunctions2_function.tasks_webhook.service_config[0].uri
    }))
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${var.tasks_escalate_token}"
    }
  }
}
```

In `terraform/terraform.tfvars.example`, add a placeholder alongside the existing ones:

```hcl
# Projects this service manages: a webhook is registered for each, and its
# completions (Done move, recurrence) are handled. "done" is that project's
# Done section gid, or null for no Done move. Real gids live in
# terraform.tfvars, never here.
asana_managed_projects = "{\"1200000000000001\":{\"done\":\"1200000000000002\"},\"1200000000000003\":{\"done\":null}}"
```

In `.github/workflows/deploy.yml`, add to the apply step's `env` block:

```yaml
          TF_VAR_asana_managed_projects: ${{ vars.ASANA_MANAGED_PROJECTS }}
```

- [ ] **Step 3: Update the prose**

In `main.py`'s module docstring, extend the webhook line and the env list:

```
webhook — HTTP trigger (public); Asana webhook handshake + completion events,
          POST /escalate for the Cloud Scheduler overdue scan, POST /digest
          for the due-day digest, and POST /webhook-sync for per-project
          webhook reconciliation.
```

```
  ASANA_WEBHOOK_SECRET                       — HMAC key for the legacy single-project webhook (webhook CF)
  ASANA_MANAGED_PROJECTS                     — {project gid: {done: section gid}} — managed set + Done mapping
  ASANA_ESCALATE_TOKEN                       — bearer token for POST /escalate, /digest and /webhook-sync (webhook CF)
```

In `docs/asana-webhook-setup.md`, add a section at the top saying the reconciler is now the primary path — `POST /webhook-sync` registers one webhook per project in `ASANA_MANAGED_PROJECTS` and stores each secret in the `asana_webhooks` table — and that the manual `scripts/register_webhook.py` runbook below it remains only for the legacy default-project webhook and for recovery.

In `CLAUDE.md`, under **Recurring tasks**, replace the paragraph beginning "The successor copies name, description…" sentence about the single project with:

```markdown
The successor copies name, description, section, tags and assignee — not
comments, subtasks, attachments or time-of-day — and lands wherever its
predecessor lived: the same projects for a top-level task, the same parent
(unsectioned) for a subtask. `repeat:` fires in any project listed in
`ASANA_MANAGED_PROJECTS`, each of which has its own Asana webhook registered
and reconciled daily by `POST /webhook-sync` (Cloud Scheduler
`tasks-webhook-sync`), with its own `X-Hook-Secret` in the `asana_webhooks`
table. A project outside that map still never fires.
```

Add `asana_webhooks` to the **Database** table's list of tables, `tasks-webhook-sync` to the scheduler entries in the **Stack** table, and note under **Secrets** that per-project webhook secrets live in Postgres rather than Secret Manager, deliberately (D3). Mark the spec's status line implemented once Task 10 is done.

- [ ] **Step 4: Validate**

Run:
```bash
cd terraform && terraform fmt -check && terraform validate; cd ..
.venv/bin/pytest tests/ -q
```
Expected: fmt clean, validate succeeds, tests pass.

- [ ] **Step 5: Commit**

```bash
git add terraform/ .github/workflows/deploy.yml main.py docs/asana-webhook-setup.md CLAUDE.md
git commit -m "feat: configure and schedule per-project webhook reconciliation"
```

---

### Task 10: Rollout

**Files:**
- Create: `scripts/test-webhook-sync.py`
- Modify: `docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md` (status line)
- Test: manual, against real Asana.

**Interfaces:**
- Consumes: everything above.
- Produces: a dry-run view of the reconciliation plan, and a verified deployment.

- [ ] **Step 1: Write the dry-run script**

Create `scripts/test-webhook-sync.py`:

```python
#!/usr/bin/env python3
"""Show what the webhook reconciler would do, without changing anything.

  .venv/bin/python scripts/test-webhook-sync.py --target <tasks-webhook-cf-url>

Reads ASANA_API_KEY, ASANA_MANAGED_PROJECTS and the Postgres vars from .env
(scripts/fetch-env.sh). Read-only: it lists webhooks and secret rows and
prints the diff. To actually reconcile, POST /webhook-sync on the deployed CF.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.asana as asana
from clients.db import get_conn
from repo import asana_webhooks as repo_webhooks
from services import managed_projects, webhook_registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="tasks-webhook CF base URL")
    args = parser.parse_args()

    managed = managed_projects.gids()
    print(f"Managed projects ({len(managed)}): {', '.join(sorted(managed)) or '(none)'}")

    registered = {}
    for hook in asana.list_webhooks():
        project_gid = webhook_registry.target_project(hook.get("target") or "", args.target)
        if project_gid:
            registered[project_gid] = hook["gid"]
        else:
            print(f"  ignoring webhook {hook['gid']} — target {hook.get('target')!r}")
    print(f"Registered for us ({len(registered)}): {registered or '(none)'}")

    with get_conn() as conn:
        with_secrets = {row["project_gid"] for row in repo_webhooks.list_all(conn)}
    print(f"Secret rows ({len(with_secrets)}): {', '.join(sorted(with_secrets)) or '(none)'}")

    plan = webhook_registry.plan(managed, registered, with_secrets)
    print(f"\nWould delete: {plan.to_delete or '(nothing)'}")
    print(f"Would register: {plan.to_register or '(nothing)'}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify the script runs read-only**

Run: `.venv/bin/python scripts/test-webhook-sync.py --target "$(gcloud functions describe tasks-webhook --project=bens-project-462804 --region=us-central1 --format='value(serviceConfig.uri)')"`
Expected: prints the managed set (empty until step 5), the legacy webhook listed as ignored, and "Would register: (nothing)". No Asana writes.

- [ ] **Step 3: Migrate the table**

Run:
```bash
scripts/fetch-env.sh
(set -a; source .env; set +a; .venv/bin/python scripts/migrate_db.py)
```
Expected: `Migration complete`. `asana_webhooks` now exists; `CREATE TABLE IF NOT EXISTS` leaves every other table untouched.

- [ ] **Step 4: Open the PR and deploy**

Use the `/pr-open` skill. Merging to `main` auto-deploys both CFs. At this point `ASANA_MANAGED_PROJECTS` is still `{}`, so nothing changes behaviorally: the legacy webhook keeps validating against the env secret (D7) and `sections.done()` still resolves to `ASANA_SECTION_DONE_GID`.

- [ ] **Step 5: Populate the managed map and apply**

Add `asana_managed_projects` to `terraform/terraform.tfvars` with the real project and Done-section gids for all five projects, and set the same JSON as the `ASANA_MANAGED_PROJECTS` GitHub repo variable. Then run the `/terraform-plan` skill, confirm the diff is the CF env var plus the new scheduler job, and run `/terraform-apply`.

- [ ] **Step 6: Reconcile once by hand and verify**

Run:
```bash
URL=$(gcloud functions describe tasks-webhook --project=bens-project-462804 --region=us-central1 --format='value(serviceConfig.uri)')
TOKEN=$(grep '^tasks_escalate_token' terraform/terraform.tfvars | cut -d'"' -f2)
curl -sS -X POST "$URL/webhook-sync" -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d "{\"target\": \"$URL\"}"
```
Expected: `{"managed": 5, "registered": 5, "deleted": 0, "active": 5}` (or `registered: 4` if Ben's Board is left to the legacy webhook — see step 8). Then confirm one row per managed project:
```bash
.venv/bin/python scripts/test-webhook-sync.py --target "$URL"
```
Expected: "Would delete: (nothing)", "Would register: (nothing)".

Check the metric with the `querying-grafana-metrics` skill: `asana_webhooks_active` should equal the managed-project count.

- [ ] **Step 7: Verify recurrence end-to-end**

In a non-default managed project, create a task tagged `repeat:1d`, complete it, and confirm the successor appears **in that same project** due tomorrow, carrying the tag, with the predecessor's tag stripped and a "↻ Next occurrence" comment posted. Repeat with a subtask under a parent task: the successor must appear under the same parent, unsectioned, and the completed subtask must not be moved to any Done section. Use the `fetch-tasks-logs` skill if either does not appear.

- [ ] **Step 8: Commit and record**

```bash
git add docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md scripts/test-webhook-sync.py
git commit -m "docs: mark cross-project recurrence implemented"
```

Set the spec's status line to `implemented — PR #NN, merged as <sha> on <date>`.

**Follow-up, deliberately not in this plan (spec Rollout step 6):** delete the legacy Ben's Board webhook, let the reconciler register a project-scoped one, then remove the `ASANA_WEBHOOK_SECRET` env fallback from `signature_valid`, the `asana_webhook_secret` Terraform variable and secret, and `scripts/register_webhook.py`. Keeping it separate is what makes this rollout revertible. Also remove the inert-`repeat:`-tag note from the Pacifica registration renewal task.

---

## Notes for the executor

- **The `repeat:` tag on a task in an unmanaged project still never fires.** That is the design (D2), not a bug — the map is the whole control surface.
- **Do not add project or section gids to any file in this repo**, including test fixtures that look real. Tests use `p-ben`, `p-family`, `sec-done`.
- **`tests/conftest.py` unsets every Postgres env var** for the whole suite. Tests that need a connection monkeypatch `get_conn` on the module under test, never on `clients.db`.
- **`handlers/asana_webhook._secret_cache` is module-level state.** Any test touching it must clear it in a fixture, or a cached secret leaks into the next test.
- **Order matters in the reconciler**: deletes before registrations. `webhook_registry.plan` deliberately returns a project in both lists when its webhook has no secret row.
- **Deliberate deviation from the spec's Testing section.** The spec asks for a
  scripts/ smoke that registers a webhook against a scratch project and deletes
  it. `scripts/test-webhook-sync.py` is read-only instead: creating a scratch
  project to prove registration works costs more than doing it against the real
  managed set in Task 10 step 6, which exercises the same path and is already a
  required rollout step.
- **Watch after rollout.** Every task event in all five projects now reaches the
  service (spec Consequences). `_MAX_REFRESH_PER_DELIVERY = 20` in
  `handlers/asana_webhook.py` bounds per-delivery index work; if the cap-exceeded
  warning starts appearing in the logs, raise it or let the backfill script heal
  the remainder. Nothing in this plan changes it.

# planning-project-tasks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the API surface to inspect Asana state and create projects/subtasks, then a `planning-project-tasks` skill that turns a prose brief into a reviewed set of Asana tasks.

**Architecture:** Minimal additive changes to `tasks-api` — new read endpoints (`GET /projects`, `GET /tags`) and write paths (`POST /projects`, a `parent` field on `POST /tasks`). The decomposition intelligence lives entirely in a new Claude Code skill that orchestrates these primitives; it inspects state, decides the container, proposes a plan for one approval, then creates tasks.

**Tech Stack:** Python 3.13, FastAPI (`api/`), httpx to Asana REST, pytest with `fastapi.testclient` and monkeypatched `clients.asana`. Skill is a Markdown `SKILL.md`.

## Global Constraints

- **Layer rules:** `clients/asana.py` is I/O only; every Asana call goes through `clients/asana._request(...)` with an `operation=` label. Routers (`api/routers/`) are thin transport, called only from `api/main.py`, and wrap Asana I/O in `with translate_asana_errors():`.
- **Auth:** every API route depends on `verify_token` (`from api.auth import verify_token`, `_: None = Depends(verify_token)`).
- **No new secrets, no infra changes.** API ships via `deploy-api.yml` (Cloud Run) on merge to `main`.
- **Subtasks are never sectioned** and never carry `projects` — they belong to their parent.
- **Test command:** `.venv/bin/pytest tests/ -q` (run subsets with `-k` or explicit paths).
- **Branch:** work is on `feat/planning-project-tasks`; open a PR via `/pr-open`, never commit to `main`.

---

## File Structure

- `clients/asana.py` — add `list_tags()` and `create_project()`. (I/O only.)
- `api/routers/tasks.py` — add `parent` to `CreateTaskRequest`; branch `create_task` on it.
- `api/routers/projects.py` (new) — `GET /projects`, `GET /tags`, `POST /projects`.
- `api/main.py` — register the projects router.
- `tests/test_asana_client.py` — tests for `list_tags`, `create_project` (+ a `_capture_seq` helper).
- `tests/test_api_tasks.py` — test for subtask creation via `parent`.
- `tests/test_api_projects.py` (new) — tests for the three project/tag endpoints.
- `~/.claude/skills/planning-project-tasks/SKILL.md` (new, outside this repo) — the skill.
- `CLAUDE.md` — document the new endpoints.
- `~/.claude/projects/-Users-ben-src-tasks/memory/tasks-api-service.md` (outside repo) — note new endpoints.

---

### Task 1: `list_tags` client function

**Files:**
- Modify: `clients/asana.py` (add after `list_projects`, ~line 251)
- Test: `tests/test_asana_client.py`

**Interfaces:**
- Consumes: existing `_paginate(path, params, *, operation)`, `get_workspace_gid()`.
- Produces: `list_tags() -> list[dict]` returning `[{"gid": str, "name": str}, ...]` — every tag in the workspace.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_asana_client.py` (it already has `_resp`, `_capture`, and the `configure` autouse fixture at the top):

```python
def test_list_tags_returns_workspace_tags(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture(
        monkeypatch,
        _resp(200, {"data": [{"gid": "t1", "name": "home"},
                             {"gid": "t2", "name": "urgent"}], "next_page": None}),
    )
    assert asana.list_tags() == [
        {"gid": "t1", "name": "home"},
        {"gid": "t2", "name": "urgent"},
    ]
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"].endswith("/workspaces/ws-1/tags")
    assert calls[0]["params"]["opt_fields"] == "name"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_asana_client.py::test_list_tags_returns_workspace_tags -v`
Expected: FAIL with `AttributeError: module 'clients.asana' has no attribute 'list_tags'`

- [ ] **Step 3: Write minimal implementation**

Add to `clients/asana.py` immediately after `list_projects` (after ~line 251):

```python
def list_tags() -> list[dict]:
    """All tags in the workspace: [{gid, name}]."""
    return _paginate(
        f"/workspaces/{get_workspace_gid()}/tags",
        {"opt_fields": "name"},
        operation="list_tags",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_asana_client.py::test_list_tags_returns_workspace_tags -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add clients/asana.py tests/test_asana_client.py
git commit -m "clients/asana: list_tags — workspace tag vocabulary"
```

---

### Task 2: `create_project` client function

**Files:**
- Modify: `clients/asana.py` (add after `list_tags`)
- Test: `tests/test_asana_client.py`

**Interfaces:**
- Consumes: `_request(...)`, `get_workspace_gid()`.
- Produces: `create_project(name: str, sections: list[str] | None = None) -> dict` returning `{"gid": str, "permalink_url": str | None, "sections": {section_name: section_gid}}`. Creates the project in the workspace, then each section in order.

- [ ] **Step 1: Write the failing test**

Add a sequenced-response helper and the test to `tests/test_asana_client.py`:

```python
def _capture_seq(monkeypatch, responses):
    """Like _capture but returns responses in order, one per call."""
    calls = []
    it = iter(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return next(it)

    monkeypatch.setattr(asana.httpx, "request", fake_request)
    return calls


def test_create_project_creates_project_then_sections(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(
        monkeypatch,
        [
            _resp(201, {"data": {"gid": "proj-new",
                                 "permalink_url": "https://app.asana.com/x/proj-new"}}),
            _resp(201, {"data": {"gid": "sec-a", "name": "Planning"}}),
            _resp(201, {"data": {"gid": "sec-b", "name": "Build"}}),
        ],
    )

    result = asana.create_project("Kitchen Remodel", ["Planning", "Build"])

    assert result["gid"] == "proj-new"
    assert result["permalink_url"] == "https://app.asana.com/x/proj-new"
    assert result["sections"] == {"Planning": "sec-a", "Build": "sec-b"}
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"].endswith("/projects")
    assert calls[0]["json"]["data"] == {"name": "Kitchen Remodel", "workspace": "ws-1"}
    assert calls[1]["url"].endswith("/projects/proj-new/sections")
    assert calls[1]["json"]["data"] == {"name": "Planning"}
    assert calls[2]["json"]["data"] == {"name": "Build"}


def test_create_project_no_sections(monkeypatch):
    monkeypatch.setattr(asana, "_workspace_gid", "ws-1")
    calls = _capture_seq(
        monkeypatch,
        [_resp(201, {"data": {"gid": "proj-x", "permalink_url": None}})],
    )
    result = asana.create_project("Solo")
    assert result["gid"] == "proj-x"
    assert result["sections"] == {}
    assert len(calls) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_asana_client.py -k create_project -v`
Expected: FAIL with `AttributeError: module 'clients.asana' has no attribute 'create_project'`

- [ ] **Step 3: Write minimal implementation**

Add to `clients/asana.py` after `list_tags`:

```python
def create_project(name: str, sections: list[str] | None = None) -> dict:
    """Create a workspace project and its sections in order.
    Returns {gid, permalink_url, sections: {name: gid}}."""
    resp = _request(
        "POST",
        "/projects",
        operation="create_project",
        params={"opt_fields": "gid,permalink_url"},
        json={"data": {"name": name, "workspace": get_workspace_gid()}},
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    project_gid = data["gid"]

    section_gids: dict[str, str] = {}
    for section_name in sections or []:
        s = _request(
            "POST",
            f"/projects/{project_gid}/sections",
            operation="create_section",
            params={"opt_fields": "gid,name"},
            json={"data": {"name": section_name}},
        )
        s.raise_for_status()
        sd = s.json()["data"]
        section_gids[sd["name"]] = sd["gid"]

    return {
        "gid": project_gid,
        "permalink_url": data.get("permalink_url"),
        "sections": section_gids,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_asana_client.py -k create_project -v`
Expected: PASS (both tests)

- [ ] **Step 5: Commit**

```bash
git add clients/asana.py tests/test_asana_client.py
git commit -m "clients/asana: create_project — project + ordered sections"
```

---

### Task 3: `parent` field on `POST /tasks` (subtask creation)

**Files:**
- Modify: `api/routers/tasks.py` (`CreateTaskRequest` ~line 47; `create_task` ~lines 194-221)
- Test: `tests/test_api_tasks.py`

**Interfaces:**
- Consumes: existing `asana.create_task_from_fields(fields) -> CreatedTask`, `_render_content`, `tags_service.resolve_gids`, `_resolve_project_gid`, `resolve_section`, `asana.add_task_to_section`.
- Produces: `POST /tasks` accepts optional `parent` (task GID). When set, the task is created as a subtask: `fields` includes `parent`, omits `projects`, and no section placement occurs. When unset, behavior is unchanged.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_api_tasks.py` (it already defines `client`, `AUTH`, the `token`/`no_db` fixtures, and imports `asana`, `CreatedTask`, `pytest`):

```python
def test_create_subtask_with_parent(monkeypatch):
    captured: dict = {}

    def fake_create(fields):
        captured.update(fields)
        return CreatedTask(gid="sub-1", permalink_url="https://app.asana.com/x/sub-1")

    monkeypatch.setattr(asana, "create_task_from_fields", fake_create)
    monkeypatch.setattr(
        asana, "add_task_to_section",
        lambda *a, **k: pytest.fail("subtask must not be sectioned"),
    )
    monkeypatch.setattr(
        asana, "list_projects",
        lambda: pytest.fail("subtask must not resolve a project"),
    )

    resp = client.post(
        "/tasks", headers=AUTH,
        json={"name": "Rent dumpster", "parent": "t-parent", "key_points": ["book online"]},
    )

    assert resp.status_code == 201
    assert resp.json()["task_gid"] == "sub-1"
    assert captured["parent"] == "t-parent"
    assert "projects" not in captured
    assert "<body>" in captured["html_notes"]  # rendered content still applies
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_api_tasks.py::test_create_subtask_with_parent -v`
Expected: FAIL — currently `create_task` calls `_resolve_project_gid` (hits the `list_projects` guard) because `parent` is ignored.

- [ ] **Step 3: Write minimal implementation**

In `api/routers/tasks.py`, add the field to `CreateTaskRequest` (after `assignee`, ~line 59):

```python
    parent: str | None = None  # task GID; when set, created as a subtask (no project/section)
```

Replace the body of `create_task` (~lines 195-221) with:

```python
def create_task(body: CreateTaskRequest, _: None = Depends(verify_token)) -> CreatedTaskResponse:
    title = _title(body.name, body.priority)  # validates priority before any Asana I/O
    with translate_asana_errors():
        fields: dict = {
            "name": title,
            "html_notes": _render_content(body),
        }
        if body.due_on is not None:
            fields["due_on"] = body.due_on
        if body.due_at is not None:
            fields["due_at"] = body.due_at
        if body.assignee is not None:
            fields["assignee"] = body.assignee
        if body.tags:
            gids = tags_service.resolve_gids(body.tags)
            if gids:
                fields["tags"] = gids

        if body.parent is not None:
            # Subtask: belongs to its parent, not a project section.
            fields["parent"] = body.parent
            created = asana.create_task_from_fields(fields)
        else:
            project_gid = _resolve_project_gid(body.project)
            fields["projects"] = [project_gid]
            section_gid = resolve_section(project_gid, body.section) if body.section else None
            created = asana.create_task_from_fields(fields)
            if section_gid:
                asana.add_task_to_section(created.gid, section_gid)

    return CreatedTaskResponse(task_gid=created.gid, permalink_url=created.permalink_url)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_tasks.py -v`
Expected: PASS — the new subtask test and all pre-existing top-level task tests (the non-`parent` branch is unchanged behavior).

- [ ] **Step 5: Commit**

```bash
git add api/routers/tasks.py tests/test_api_tasks.py
git commit -m "tasks-api: POST /tasks accepts parent → subtask creation"
```

---

### Task 4: projects router — `GET /projects`, `GET /tags`, `POST /projects`

**Files:**
- Create: `api/routers/projects.py`
- Modify: `api/main.py` (imports + `include_router`, ~lines 46-49)
- Test: `tests/test_api_projects.py` (new)

**Interfaces:**
- Consumes: `asana.list_projects()`, `asana.get_sections(gid)`, `asana.list_tags()` (Task 1), `asana.create_project(name, sections)` (Task 2), `verify_token`, `translate_asana_errors`.
- Produces:
  - `GET /projects` → `{"projects": [{"gid","name","sections":[{"gid","name"}]}]}`
  - `GET /tags` → `{"tags": [{"gid","name"}]}`
  - `POST /projects` body `{"name": str, "sections": [str]}` → 201 `{"project_gid","permalink_url","sections":{name:gid}}`

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_projects.py`:

```python
import pytest
from fastapi.testclient import TestClient

import clients.asana as asana
from api.main import app

client = TestClient(app)
AUTH = {"Authorization": "Bearer sekrit"}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")


def test_get_projects_includes_sections(monkeypatch):
    monkeypatch.setattr(asana, "list_projects",
                        lambda: [{"gid": "p1", "name": "Home"}])
    monkeypatch.setattr(asana, "get_sections",
                        lambda gid: [{"gid": "s1", "name": "Planning"}])

    resp = client.get("/projects", headers=AUTH)
    assert resp.status_code == 200
    project = resp.json()["projects"][0]
    assert project["name"] == "Home"
    assert project["sections"] == [{"gid": "s1", "name": "Planning"}]


def test_get_tags(monkeypatch):
    monkeypatch.setattr(asana, "list_tags",
                        lambda: [{"gid": "t1", "name": "home"}])
    resp = client.get("/tags", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["tags"] == [{"gid": "t1", "name": "home"}]


def test_create_project(monkeypatch):
    monkeypatch.setattr(
        asana, "create_project",
        lambda name, sections: {
            "gid": "p-new",
            "permalink_url": "https://app.asana.com/x/p-new",
            "sections": {"Planning": "s1"},
        },
    )
    resp = client.post("/projects", headers=AUTH,
                       json={"name": "Reno", "sections": ["Planning"]})
    assert resp.status_code == 201
    body = resp.json()
    assert body["project_gid"] == "p-new"
    assert body["permalink_url"] == "https://app.asana.com/x/p-new"
    assert body["sections"] == {"Planning": "s1"}


def test_projects_requires_auth():
    assert client.get("/projects").status_code in (401, 403)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_api_projects.py -v`
Expected: FAIL — `GET /projects` returns 404 (route not registered yet).

- [ ] **Step 3: Write minimal implementation**

Create `api/routers/projects.py`:

```python
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import clients.asana as asana
from api.auth import verify_token
from api.errors import translate_asana_errors

router = APIRouter()


class SectionInfo(BaseModel):
    gid: str
    name: str


class ProjectInfo(BaseModel):
    gid: str
    name: str
    sections: list[SectionInfo] = []


class ProjectsResponse(BaseModel):
    projects: list[ProjectInfo]


class TagInfo(BaseModel):
    gid: str
    name: str


class TagsResponse(BaseModel):
    tags: list[TagInfo]


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1)
    sections: list[str] = []


class CreatedProjectResponse(BaseModel):
    project_gid: str
    permalink_url: str | None = None
    sections: dict[str, str] = {}


@router.get("/projects", response_model=ProjectsResponse)
def get_projects(_: None = Depends(verify_token)) -> ProjectsResponse:
    with translate_asana_errors():
        projects = asana.list_projects()
        out = [
            ProjectInfo(
                gid=p["gid"],
                name=p["name"],
                sections=[SectionInfo(gid=s["gid"], name=s["name"])
                          for s in asana.get_sections(p["gid"])],
            )
            for p in projects
        ]
    return ProjectsResponse(projects=out)


@router.get("/tags", response_model=TagsResponse)
def get_tags(_: None = Depends(verify_token)) -> TagsResponse:
    with translate_asana_errors():
        tags = asana.list_tags()
    return TagsResponse(tags=[TagInfo(gid=t["gid"], name=t["name"]) for t in tags])


@router.post("/projects", response_model=CreatedProjectResponse, status_code=201)
def create_project(
    body: CreateProjectRequest, _: None = Depends(verify_token)
) -> CreatedProjectResponse:
    with translate_asana_errors():
        result = asana.create_project(body.name, body.sections)
    return CreatedProjectResponse(
        project_gid=result["gid"],
        permalink_url=result.get("permalink_url"),
        sections=result["sections"],
    )
```

Register it in `api/main.py` — change the import line and add the `include_router`:

```python
from api.routers import comments, projects, search, tasks

app.include_router(search.router)
app.include_router(tasks.router)
app.include_router(comments.router)
app.include_router(projects.router)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api_projects.py -v`
Expected: PASS (all four)

- [ ] **Step 5: Commit**

```bash
git add api/routers/projects.py api/main.py tests/test_api_projects.py
git commit -m "tasks-api: GET /projects, GET /tags, POST /projects"
```

---

### Task 5: `planning-project-tasks` skill + docs

Skill structure follows the creating-skills standard: a lean `SKILL.md` that
**references** `editing-tasks`/`searching-tasks` instead of repeating their
curls, plus one `fast` subagent that absorbs the verbose inspection I/O and
returns a compact digest. No `references/` — the container-decision logic is the
skill's core and stays in the body (<500 words). No creation subagent — the
writes are the approval-gated, irreversible step and stay visible in the parent.

**Files:**
- Create: `~/.claude/skills/planning-project-tasks/SKILL.md` (outside this repo)
- Create: `~/.claude/skills/planning-project-tasks/agents/inspecting-asana-state.md` (outside this repo)
- Modify: `CLAUDE.md` (tasks-api endpoint description, ~the API row of the Stack table)
- Modify: `~/.claude/projects/-Users-ben-src-tasks/memory/tasks-api-service.md` (outside this repo)

**Interfaces:**
- Consumes: the endpoints from Tasks 1-4, plus existing `POST /search`, `POST /tasks`. Auth token from `~/src/tasks/terraform/terraform.tfvars` (`tasks_api_token`), base `https://tasks-api.drolet.cloud` — identical to the `editing-tasks` skill.
- Produces: a skill directory (documentation/behavior only — verified via the validate-skill subagent, no pytest). The subagent takes candidate task names + returns `{projects:[{name,sections}], tags:[...], dedup:[{candidate, matches:[{name,gid,project}]}]}`.

- [ ] **Step 1: Write the inspection subagent**

Create `~/.claude/skills/planning-project-tasks/agents/inspecting-asana-state.md`:

````markdown
---
name: inspecting-asana-state
description: Fetch Asana projects/sections/tags and dedup-search candidate task names; return a compact digest.
model: fast
---

# Inspecting Asana State

## Inputs
- A list of candidate task names (the parent's decomposition of the brief).

## Steps
1. Resolve auth:
   ```bash
   TOKEN=$(grep 'tasks_api_token' ~/src/tasks/terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
   BASE=https://tasks-api.drolet.cloud
   ```
2. `curl -s "$BASE/projects" -H "Authorization: Bearer $TOKEN"` — projects + sections.
3. `curl -s "$BASE/tags" -H "Authorization: Bearer $TOKEN"` — tag vocabulary.
4. For each candidate name, dedup-search (open + completed):
   ```bash
   curl -s -XPOST "$BASE/search" -H "Authorization: Bearer $TOKEN" \
     -H "Content-Type: application/json" -d '{"query":"<name>","completed":null}'
   ```

## Output
A compact digest ONLY (no raw JSON dumps):
- **Projects**: each project name → its section names.
- **Tags**: the existing tag names.
- **Dedup**: per candidate, any close-match existing tasks as `name (gid) — project`,
  or "no matches". Do not decide skips — just report matches.
````

- [ ] **Step 2: Write the skill file**

Create `~/.claude/skills/planning-project-tasks/SKILL.md`:

````markdown
---
name: planning-project-tasks
version: 1.0.0
description: >
  Use when the user hands over a project brief or a detailed list of things to
  be done and wants them created as Asana tasks — "plan this project", "turn
  this into tasks", "ingest these into Asana", "break this down into tasks". For
  creating or editing a single task, use editing-tasks instead.
metadata:
  depends-on: "searching-tasks, editing-tasks"
---

# Planning Project Tasks

Turn a prose brief into a reviewed set of Asana tasks. You decide the structure
from what already exists; you always show the plan before writing anything.

## 1. Understand the brief

Draft the candidate task breakdown. Ask clarifying questions ONLY where it is
genuinely ambiguous: scope boundaries, hard deadlines, or priority. Don't over-ask.

## 2. Inspect existing state

Dispatch the **inspecting-asana-state** subagent (`agents/inspecting-asana-state.md`)
with your candidate task names. It returns a digest: existing projects + their
sections, the tag vocabulary, and per-candidate duplicate matches. (Fallback if
dispatch fails: run the curls in that agent file inline.)

## 3. Decide the container (your call)

Pick ONE from the digest:

- **New project** — a big or distinct initiative with no good existing home →
  create a project with sections.
- **Nest in an existing project** — the brief clearly belongs to one that exists
  → tasks into its EXISTING sections (this skill can't add sections to an
  existing project).
- **Parent + subtasks** — a small list → one parent task in the default tasks
  project, items as subtasks.

Reuse existing sections and tags rather than inventing near-duplicates.

## 4. Propose the plan, get ONE approval

Show the user before any write: the container choice (names), the full task tree
(names, priorities, due dates, key points) with subtasks nested, and **dedup
flags** — for each likely duplicate, name the existing task and ask whether to
skip. Never auto-skip. Wait for explicit approval; revise if asked.

## 5. Create (orchestrate, in order)

Create tasks with the **editing-tasks** skill's `POST /tasks` (same fields). Order:

1. If a new project is warranted → `POST /projects` with
   `{"name":..., "sections":[...]}`; keep the returned `sections` name→gid map.
2. Each top-level task → `POST /tasks` into its section (omit `parent`).
3. Each subtask → `POST /tasks` with `parent` = the parent's `task_gid` and NO
   `project`/`section` (a subtask belongs to its parent, never a section).

Report each created `permalink_url`. There is no rollback — if a call fails
partway, list what was created vs. failed and offer to retry the remainder.
````

- [ ] **Step 3: Validate the skill**

Read `~/.claude/skills/creating-skills/agents/validate-skill.md`, then spawn the
**validate-skill** subagent with the absolute path
`~/.claude/skills/planning-project-tasks`. Fix any **FAIL** items; address
**WARN** items if practical.

Expected: no FAIL items (name matches directory, description is triggers-only,
body under 500 words, dependencies declared, subagent has Inputs/Steps/Output).

- [ ] **Step 4: Update in-repo docs**

In `CLAUDE.md`, extend the `tasks-api` row of the Stack table so it lists the new endpoints. Change the API cell text from:

```
search/fetch/add/update for tasks + comments
```

to:

```
search/fetch/add/update for tasks + comments; list/create projects, list tags, subtasks
```

- [ ] **Step 5: Update the memory pointer (outside repo)**

Append to `~/.claude/projects/-Users-ben-src-tasks/memory/tasks-api-service.md` a line noting the new endpoints:

```
- `GET /projects` (with sections), `GET /tags`, `POST /projects`, and `parent` on `POST /tasks` (subtasks) back the planning-project-tasks skill.
```

- [ ] **Step 6: Commit in-repo docs**

```bash
git add CLAUDE.md
git commit -m "docs: note project/tag/subtask endpoints for planning-project-tasks"
```

(The skill files and memory file live outside this repo — no repo commit for those.)

---

### Task 6: Full test run + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS, no regressions.

- [ ] **Step 2: Manual smoke against a local server (optional, creates REAL Asana objects)**

```bash
scripts/fetch-env.sh
(set -a; source .env; set +a; .venv/bin/uvicorn api.main:app --port 8080) &
TOKEN=$(grep 'tasks_api_token' terraform/terraform.tfvars | grep -o '"[^"]*"' | tr -d '"')
curl -s localhost:8080/projects -H "Authorization: Bearer $TOKEN" | head
curl -s localhost:8080/tags -H "Authorization: Bearer $TOKEN" | head
# Subtask path (REAL writes) — only if you want end-to-end confidence:
#   POST /projects, then POST /tasks (top-level), then POST /tasks with parent.
```

Expected: `/projects` lists projects with sections; `/tags` lists tags.

- [ ] **Step 3: Open the PR**

Use the `/pr-open` skill (per repo workflow — auto-deploy watches `main`).

---

## Self-Review

**Spec coverage:**
- `GET /projects` (container + section reuse) → Task 4. ✓
- `GET /tags` (tag reuse) → Task 1 (client) + Task 4 (endpoint). ✓
- `POST /projects` (new project + sections) → Task 2 (client) + Task 4 (endpoint). ✓
- `parent` on `POST /tasks` (subtasks, unsectioned) → Task 3. ✓
- Dedup via existing `POST /search` (no code change) → skill Step 2/4. ✓
- Skill: inspect → decide container → propose → approve → orchestrate → Task 5. ✓
- Partial-failure reporting (no rollback) → skill Step 5. ✓
- Docs/memory updates → Task 5. ✓
- Tests + delivery → Task 6. ✓

**Placeholder scan:** none — every code and command step is concrete.

**Type consistency:** `create_project` returns `{"gid","permalink_url","sections"}` in Task 2 and is consumed with those exact keys in Task 4. `list_tags` returns `[{"gid","name"}]` in Task 1, consumed as such in Task 4. `create_task_from_fields(fields) -> CreatedTask` (existing) used unchanged in Task 3. Endpoint response shapes match between the skill's expectations (Task 5) and the routers (Task 4). ✓

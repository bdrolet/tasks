"""The managed-project map: which Asana projects this service acts on.

ASANA_MANAGED_PROJECTS is a JSON object keyed by project gid:

    {"<gid>": {"done": "<done section gid>"}, "<other gid>": {"done": null}}

Membership *is* the definition of "managed": a webhook is registered for the
project, its completions are handled, and its Done move happens when `done`
is non-null. One place to add a project.

Key order is significant — see project_of(). The variable is a raw JSON
string passed through Terraform verbatim, never jsonencode()d from an HCL
map, so the order written in terraform.tfvars is the order seen here.

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
        logger.warning("%s unset — treating as empty", ENV_VAR)
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
    """The project a task lives in, resolved deterministically. A subtask has
    no memberships, so it yields None — callers treat that as "no project to
    act in".

    Order matters because a task can be in several projects at once, and
    Asana returns memberships in no guaranteed order:

    1. ASANA_PROJECT_ID, whenever the task is in it. With an empty map this
       makes the feature a true no-op — a multi-homed task still resolves to
       the default project, so sections.done() still returns
       ASANA_SECTION_DONE_GID and current_section() still reads the section
       the successor should inherit, exactly as before this feature.
    2. The first managed project in the map's own declaration order — JSON
       key order, which managed() preserves. The map is the place where the
       precedence between two managed projects is declared, the same job
       ASANA_PROJECT_CALENDARS gives its `order` field; resolving by the
       task's membership order instead would hand that decision to Asana and
       make the Done section non-deterministic for a multi-homed task.
    3. Otherwise the first membership, arbitrary but harmless: a task in no
       managed project and not in the default one has no Done section
       configured either way."""
    project_gids = [
        gid for m in task.get("memberships") or [] if (gid := (m.get("project") or {}).get("gid"))
    ]
    if not project_gids:
        return None
    default = os.environ.get("ASANA_PROJECT_ID")
    if default and default in project_gids:
        return default
    present = set(project_gids)
    for gid in managed():
        if gid in present:
            return gid
    return project_gids[0]

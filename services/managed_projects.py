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

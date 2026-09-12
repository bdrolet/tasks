"""Map inbox categories / labels and lifecycle states to Asana section GIDs.

GIDs are configured via env vars (Terraform variables → CF env). To find a
section GID: open the section in Asana — the numeric ID at the end of the URL.
"""

import os

from services import managed_projects

_BY_CATEGORY = {
    "review": "ASANA_SECTION_REVIEW_GID",
    "respond": "ASANA_SECTION_RESPOND_GID",
    "urgent": "ASANA_SECTION_URGENT_GID",  # optional — unset leaves urgent tasks unsectioned
}

# Categories with no section of their own — an email the gate-1 screener
# rescued from ignore/reference — land in Review rather than unsectioned.
# Opt-in, because for_category has two callers: handlers/task_create.py passes
# a category and wants the default; handlers/label_applied.py passes a LABEL,
# where "ignore"/"reference" mean "no section move", not "move it to Review".
_DEFAULT_SECTION_VAR = "ASANA_SECTION_REVIEW_GID"


def for_category(category: str, *, default: bool = False) -> str | None:
    var = _BY_CATEGORY.get(category) or (_DEFAULT_SECTION_VAR if default else None)
    return os.environ.get(var) or None if var else None


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


def overdue() -> str | None:
    return os.environ.get("ASANA_SECTION_OVERDUE_GID") or None

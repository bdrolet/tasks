"""Map inbox categories / labels and lifecycle states to Asana section GIDs.

GIDs are configured via env vars (Terraform variables → CF env). To find a
section GID: open the section in Asana — the numeric ID at the end of the URL.
"""

import os

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


def done() -> str | None:
    return os.environ.get("ASANA_SECTION_DONE_GID") or None


def overdue() -> str | None:
    return os.environ.get("ASANA_SECTION_OVERDUE_GID") or None

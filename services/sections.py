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


def for_category(category: str) -> str | None:
    var = _BY_CATEGORY.get(category)
    return os.environ.get(var) or None if var else None


def done() -> str | None:
    return os.environ.get("ASANA_SECTION_DONE_GID") or None


def overdue() -> str | None:
    return os.environ.get("ASANA_SECTION_OVERDUE_GID") or None

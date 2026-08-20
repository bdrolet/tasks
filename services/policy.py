"""Task policy: which classified emails become Asana tasks.

Inbox classifies; this module decides. Changing what becomes a task happens
here — no inbox deploy needed. Urgent is included to match pre-extraction
inbox behavior (urgent.handle created tasks too)."""

import re

from models.events import EmailClassifiedEvent

_TASK_CATEGORIES = {"urgent", "review", "respond"}


def warrants_task(event: EmailClassifiedEvent) -> bool:
    return event.get("category") in _TASK_CATEGORIES


# Deterministic backstop for gate 2 (docs/no_action_needed_example.md): if the
# enrichment's own key points contain an explicit no-action phrase, do not
# create the task. Free to test; catches what the agent missed when the
# summarizer wrote the disqualifier down anyway.
NO_ACTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"no action (?:is )?(?:required|needed)",
        r"for your records",
        r"automatic payment",
        r"\bautopay\b",
    )
]


def no_action_phrase(key_points: list[str]) -> str | None:
    """The first no-action phrase found in the generated key points, else None."""
    for point in key_points:
        for pat in NO_ACTION_PATTERNS:
            m = pat.search(point or "")
            if m:
                return m.group(0).lower()
    return None

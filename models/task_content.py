"""Pure content types for a task's description. No imports from other layers.

TaskContent is the shared shape rendered into Asana html_notes by
services/task_content.py. Structured attributes (priority, category, topic,
due date) are NOT here — they live in native Asana slots, not the description.
JSON has no tuples, so link/row pairs are (a, b) tuples built by callers.
"""

from dataclasses import dataclass, field


@dataclass
class Source:
    """The provenance footer: where the task came from."""

    origin: str  # e.g. "Email", "Created manually"
    rows: list[tuple[str, str]] = field(default_factory=list)  # (label, value): From, Received
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, label): Open in Outlook
    note: str | None = None  # freeform provenance note (automated: AI reasoning)


@dataclass
class TaskContent:
    context: str | None = None  # optional lead prose ("what / why")
    key_points: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, label)
    action_items: list[tuple[str, str]] = field(default_factory=list)  # (label, url)
    source: Source | None = None

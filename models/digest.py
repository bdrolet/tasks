"""Pure types for the due-day digest. No imports from other layers.

A DigestTask is one Asana task after routing and bullet condensing; a
DigestEvent is the calendar event one (day, calendar) pair should show; a
Plan is the diff between desired events and the rows the DB says exist."""

import hashlib
import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DigestTask:
    gid: str
    name: str
    permalink_url: str
    due_on: str  # YYYY-MM-DD
    calendar_id: str  # "primary" or a real calendar id
    points: list[str] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, label)


@dataclass
class DigestEvent:
    day: str  # YYYY-MM-DD
    calendar_id: str
    title: str
    sections: list[dict]  # schedule-api `sections` payload, JSON-ready
    task_gids: list[str]

    def content_hash(self) -> str:
        payload = json.dumps(
            {"title": self.title, "sections": self.sections}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class Plan:
    creates: list[DigestEvent] = field(default_factory=list)
    updates: list[tuple[DigestEvent, dict]] = field(default_factory=list)  # (desired, stored row)
    deletes: list[dict] = field(default_factory=list)  # stored rows

"""Pure types for the "do next" prioritizer. No imports from other layers.
Design: docs/superpowers/specs/2026-09-23-next-prioritizer-design.md"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import ClassVar


@dataclass(frozen=True)
class TaskFacts:
    gid: str
    project_gid: str | None
    project_name: str | None
    parent_gid: str | None
    name: str
    permalink_url: str | None
    priority: str | None  # 'P0'..'P3' parsed from the title, else None
    due_on: date | None
    due_at: datetime | None
    start_on: date | None
    started_at: date | None  # "Started at" custom field
    story_points: int | None  # "Story points" custom field
    points_estimated: int | None  # what enrichment wrote, once
    completed: bool
    completed_at: datetime | None
    created_at: datetime
    modified_at: datetime
    tags: tuple[str, ...]
    dependencies: tuple[str, ...]
    dependents: tuple[str, ...]
    num_open_subtasks: int
    content_hash: str


@dataclass(frozen=True)
class Enrichment:
    story_points_suggested: int | None
    points_confidence: str
    waiting_on: str | None
    due_date_inferred: date | None
    due_date_inferred_confidence: str
    impact: str
    energy: str
    latest_comment_signal: str
    reason: str | None
    unenriched: bool

    DEFAULT: ClassVar["Enrichment"]


Enrichment.DEFAULT = Enrichment(
    story_points_suggested=None,
    points_confidence="low",
    waiting_on=None,
    due_date_inferred=None,
    due_date_inferred_confidence="low",
    impact="medium",
    energy="shallow",
    latest_comment_signal="none",
    reason=None,
    unenriched=True,
)


@dataclass(frozen=True)
class Overrides:
    fields: dict = field(default_factory=dict)
    pinned_rank: int | None = None
    snooze_until: date | None = None

    NONE: ClassVar["Overrides"]


Overrides.NONE = Overrides()


@dataclass(frozen=True)
class Stats:
    times_deferred: int = 0

    NONE: ClassVar["Stats"]


Stats.NONE = Stats()


@dataclass
class ScoredTask:
    gid: str
    bucket: str  # next | nudge | snoozed | excluded:<reason>
    score: float | None
    position: int
    rank: int | None
    components: dict
    overcommitted: bool = False
    stale: bool = False
    stale_reason: str | None = None
    project_name: str | None = None
    points: int = 0
    energy: str = "shallow"
    pinned_rank: int | None = None


@dataclass
class ScoredSet:
    today: date
    tasks: list[ScoredTask]

    def next(self) -> list[ScoredTask]:
        return [t for t in self.tasks if t.bucket == "next"]

    def by_gid(self) -> dict[str, ScoredTask]:
        return {t.gid: t for t in self.tasks}

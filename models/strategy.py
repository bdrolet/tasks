"""Pure types for the strategy layer. No imports from other layers.
Design: docs/superpowers/specs/2026-10-09-strategy-layer-design.md"""

from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

ROLES = ("path", "derisk", "support")
KINDS = ("outcome", "area")
PERIODS = ("day", "week", "month")
OPS = (">=", "<=", "=", "<", ">")
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}


@dataclass(frozen=True)
class Measure:
    """A `lead:` or `lag:` line. For a lead, `tag` is an Asana tag name; for
    a lag it is the free text before the operator."""

    tag: str
    op: str
    value: float
    period: str


@dataclass(frozen=True)
class Tripwire:
    ordinal: int  # 1-based position among the block's tripwire lines
    subject: str  # a lead tag name, or the literal "lag"
    op: str
    value: float
    by: date
    action: str


@dataclass(frozen=True)
class Signal:
    """One below-the-line signal. `cls` is "evidence" (overdue, undated) or
    "absence" (stale, lead) — only evidence signals may boost scoring."""

    kind: str  # overdue | undated | stale | lead
    cls: str
    text: str
    min_priority: str = "P1"
    grace: int = 3
    tag: str | None = None
    after: date | None = None
    days: int | None = None
    value: float | None = None
    period: str | None = None


@dataclass(frozen=True)
class Goal:
    id: str
    kind: str
    weight: float = 1.0
    horizon: date | None = None
    lag: Measure | None = None
    leads: tuple[Measure, ...] = ()
    tripwires: tuple[Tripwire, ...] = ()
    standard: str | None = None
    signals: tuple[Signal, ...] = ()
    review: str = "weekly"
    prose: str = ""


@dataclass(frozen=True)
class Strategy:
    goals: tuple[Goal, ...] = ()
    last_reviewed: date | None = None
    findings: tuple[str, ...] = ()
    text_hash: str = ""

    EMPTY: ClassVar["Strategy"]

    def get(self, goal_id: str) -> Goal | None:
        return next((g for g in self.goals if g.id == goal_id), None)

    def outcome_goals(self) -> tuple[Goal, ...]:
        return tuple(g for g in self.goals if g.kind == "outcome")

    def areas(self) -> tuple[Goal, ...]:
        return tuple(g for g in self.goals if g.kind == "area")


Strategy.EMPTY = Strategy()


@dataclass(frozen=True)
class SignalState:
    signal: str
    cls: str
    raw: bool | None  # None = no_data
    effective: bool
    consecutive_days: int
    tasks: tuple[str, ...] = ()

    @property
    def state(self) -> str:
        if self.raw is None:
            return "no_data"
        return "true" if self.effective else "false"


@dataclass(frozen=True)
class GoalState:
    goal_id: str
    kind: str
    day: date
    strategy_hash: str
    state: dict = field(default_factory=dict)

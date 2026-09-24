"""The scorer's tunables, read from config/prioritize.toml (stdlib tomllib).
PRIORITIZE_CONFIG_PATH overrides the path. Every section is required —
a missing one is a KeyError naming it, not a silent default."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "prioritize.toml"


@dataclass(frozen=True)
class Config:
    points_per_day: float
    default_points: int
    low_confidence_multiplier: float
    min_effort_days: float
    weights: dict[str, float]
    priority_weight: dict[str, float]
    default_priority: str
    horizon_days: dict[str, int]
    urgency_k: float
    urgency_s0: float
    soft_cap: float
    no_due_urgency: float
    impact_weight: dict[str, float]
    stale_days: int
    unblock_per_task: float
    category_weight: dict[str, float]
    default_category_weight: float
    default_n: int
    diversity_penalty: float
    energy_penalty: float
    stale_after_days: int
    deferred_limit: int


def load(path: str | None = None) -> Config:
    p = Path(path or os.environ.get("PRIORITIZE_CONFIG_PATH") or DEFAULT_PATH)
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    cap, weights, prio = raw["capacity"], raw["weights"], raw["priority"]
    urg, cat, sel, stale = raw["urgency"], raw["category"], raw["selection"], raw["stale"]
    return Config(
        points_per_day=float(cap["points_per_day"]),
        default_points=int(cap["default_points"]),
        low_confidence_multiplier=float(cap["low_confidence_multiplier"]),
        min_effort_days=float(cap["min_effort_days"]),
        weights={k: float(v) for k, v in weights.items()},
        priority_weight={k: float(v) for k, v in prio.items() if k != "default"},
        default_priority=str(prio["default"]),
        horizon_days={k: int(v) for k, v in raw["horizon_days"].items()},
        urgency_k=float(urg["k"]),
        urgency_s0=float(urg["s0"]),
        soft_cap=float(urg["soft_cap"]),
        no_due_urgency=float(urg["no_due"]),
        impact_weight={k: float(v) for k, v in raw["impact"].items()},
        stale_days=int(raw["aging"]["stale_days"]),
        unblock_per_task=float(raw["unblock"]["per_task"]),
        category_weight={k: float(v) for k, v in cat.items() if k != "default"},
        default_category_weight=float(cat["default"]),
        default_n=int(sel["default_n"]),
        diversity_penalty=float(sel["diversity_penalty"]),
        energy_penalty=float(sel["energy_penalty"]),
        stale_after_days=int(stale["after_days"]),
        deferred_limit=int(stale["deferred_limit"]),
    )

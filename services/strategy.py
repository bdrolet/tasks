# services/strategy.py
"""Parse the `## Strategy` section of the standing context into goals and
areas. Pure: `parse` takes text; `load` reads the section through
services.standing_context. Lenient by design — a bad block or line is a
finding, never an exception (spec D3).
Design: docs/superpowers/specs/2026-10-09-strategy-layer-design.md (D2, D3, D9)"""

import hashlib
import logging
import re
from datetime import date

from models.strategy import (
    KINDS,
    OPS,
    Goal,
    Measure,
    Signal,
    Strategy,
    Tripwire,
)
from services import standing_context

logger = logging.getLogger(__name__)

SECTION = "Strategy"
_ID_RE = re.compile(r"^[a-z0-9-]+$")
_HEADER_RE = re.compile(r"^- ([a-z][a-z -]*?):\s*(.*)$")
_OPS = "|".join(re.escape(o) for o in sorted(OPS, key=len, reverse=True))
_NUM = r"(\d+(?:\.\d+)?)"
_DATE = r"(\d{4}-\d{2}-\d{2})"
_LEAD_RE = re.compile(rf"^(\S+)\s*({_OPS})\s*{_NUM}\s+per\s+(day|week|month)$")
_LAG_RE = re.compile(rf"^(.+?)\s*({_OPS})\s*{_NUM}\s+per\s+(day|week|month)$")
_TRIP_RE = re.compile(rf"^(\S+)\s*({_OPS})\s*{_NUM}\s+by\s+{_DATE}\s*->\s*(.+)$")
_OVERDUE_RE = re.compile(r"^overdue(?::P([0-3])\+)?(?:\s+grace\s+(\d+))?$")
_OVERDUE_TAG_RE = re.compile(r"^overdue:([a-z0-9][a-z0-9_-]*)(?:\s+grace\s+(\d+))?$")
_UNDATED_RE = re.compile(rf"^undated:(\S+)\s+after\s+{_DATE}$")
_STALE_RE = re.compile(r"^stale\s*>\s*(\d+)\s+days?$")
_LEAD_SIG_RE = re.compile(rf"^lead\s+(\S+)\s*<\s*{_NUM}\s+per\s+(day|week|month)$")


def text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode()).hexdigest()


def _date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_signal(text: str) -> Signal | None:
    t = text.strip()
    if m := _OVERDUE_RE.match(t):
        return Signal(
            kind="overdue",
            cls="evidence",
            text=t,
            min_priority=f"P{m.group(1)}" if m.group(1) else "P1",
            grace=int(m.group(2)) if m.group(2) else 3,
        )
    if m := _OVERDUE_TAG_RE.match(t):
        return Signal(
            kind="overdue",
            cls="evidence",
            text=t,
            min_priority="P3",  # a tag signal applies no priority floor
            grace=int(m.group(2)) if m.group(2) else 3,
            tag=m.group(1),
        )
    if m := _UNDATED_RE.match(t):
        return Signal(
            kind="undated", cls="evidence", text=t, tag=m.group(1), after=_date(m.group(2))
        )
    if m := _STALE_RE.match(t):
        return Signal(kind="stale", cls="absence", text=t, days=int(m.group(1)))
    if m := _LEAD_SIG_RE.match(t):
        return Signal(
            kind="lead",
            cls="absence",
            text=t,
            tag=m.group(1),
            value=float(m.group(2)),
            period=m.group(3),
        )
    return None


def _split_blocks(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(preamble lines, [(id, block body)])."""
    preamble: list[str] = []
    blocks: list[tuple[str, str]] = []
    current_id: str | None = None
    current: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith("### "):
            if current_id is not None:
                blocks.append((current_id, "\n".join(current)))
            current_id, current = line[4:].strip(), []
        elif current_id is None:
            preamble.append(line)
        else:
            current.append(line)
    if current_id is not None:
        blocks.append((current_id, "\n".join(current)))
    return preamble, blocks


def _parse_block(goal_id: str, body: str, findings: list[str]) -> Goal | None:
    header: list[tuple[str, str]] = []
    prose_lines: list[str] = []
    in_header = True
    for line in body.splitlines():
        m = _HEADER_RE.match(line) if in_header else None
        if m:
            header.append((m.group(1).strip(), m.group(2).strip()))
        elif in_header and not line.strip() and not header:
            continue
        else:
            in_header = False
            prose_lines.append(line)
    fields: dict = {"id": goal_id, "prose": "\n".join(prose_lines).strip()}
    leads: list[Measure] = []
    tripwires: list[Tripwire] = []
    signals: list[Signal] = []
    for key, value in header:
        try:
            if key == "kind":
                if value not in KINDS:
                    findings.append(f"{goal_id}: unknown kind {value!r}")
                    return None
                fields["kind"] = value
            elif key == "weight":
                fields["weight"] = float(value)
            elif key == "horizon":
                fields["horizon"] = _date(value)
            elif key == "lag":
                m = _LAG_RE.match(value)
                if not m:
                    raise ValueError(value)
                fields["lag"] = Measure(
                    m.group(1).strip(), m.group(2), float(m.group(3)), m.group(4)
                )
            elif key == "lead":
                m = _LEAD_RE.match(value)
                if not m:
                    raise ValueError(value)
                leads.append(Measure(m.group(1), m.group(2), float(m.group(3)), m.group(4)))
            elif key == "tripwire":
                m = _TRIP_RE.match(value)
                if not m:
                    raise ValueError(value)
                tripwires.append(
                    Tripwire(
                        ordinal=len(tripwires) + 1,
                        subject=m.group(1),
                        op=m.group(2),
                        value=float(m.group(3)),
                        by=_date(m.group(4)),
                        action=m.group(5).strip(),
                    )
                )
            elif key == "standard":
                fields["standard"] = value
            elif key == "below-the-line":
                for part in value.split(";"):
                    try:
                        sig = _parse_signal(part)
                    except ValueError:
                        findings.append(
                            f"{goal_id}: below-the-line signal {part.strip()!r} has an invalid date"
                        )
                        continue
                    if sig is None:
                        findings.append(
                            f"{goal_id}: below-the-line signal {part.strip()!r} not understood"
                        )
                    else:
                        signals.append(sig)
            elif key == "review":
                if value not in ("weekly", "monthly"):
                    raise ValueError(value)
                fields["review"] = value
            else:
                findings.append(f"{goal_id}: unknown header {key!r}")
        except ValueError:
            findings.append(f"{goal_id}: {key} line {value!r} not understood")
    if "kind" not in fields:
        findings.append(f"{goal_id}: missing kind")
        return None
    return Goal(leads=tuple(leads), tripwires=tuple(tripwires), signals=tuple(signals), **fields)


def parse(text: str, *, stale_after_days: int = 90, today: date | None = None) -> Strategy:
    if not (text or "").strip():
        return Strategy.EMPTY
    today = today or date.today()
    findings: list[str] = []
    preamble, blocks = _split_blocks(text)
    last_reviewed: date | None = None
    for line in preamble:
        m = _HEADER_RE.match(line)
        if m and m.group(1) == "last reviewed":
            try:
                last_reviewed = _date(m.group(2))
            except ValueError:
                findings.append(f"last reviewed {m.group(2)!r} not understood")
    goals: list[Goal] = []
    seen: set[str] = set()
    for goal_id, body in blocks:
        if not _ID_RE.match(goal_id):
            findings.append(f"{goal_id}: id must match [a-z0-9-]+")
            continue
        if goal_id in seen:
            findings.append(f"duplicate id {goal_id}; keeping the first")
            continue
        g = _parse_block(goal_id, body, findings)
        if g is None:
            continue
        seen.add(goal_id)
        goals.append(g)
    for g in goals:
        if g.kind == "outcome" and not g.tripwires:
            findings.append(f"{g.id}: outcome goal has no tripwire")
        if g.kind == "outcome" and not g.leads:
            findings.append(f"{g.id}: outcome goal has no lead measure")
    if last_reviewed is None:
        findings.append("strategy has no 'last reviewed' line")
    elif (today - last_reviewed).days > stale_after_days:
        findings.append(f"strategy last reviewed {(today - last_reviewed).days} days ago")
    for f in findings:
        logger.warning("strategy: %s", f)
    return Strategy(
        goals=tuple(goals),
        last_reviewed=last_reviewed,
        findings=tuple(findings),
        text_hash=text_hash(text),
    )


def section_text() -> str:
    return standing_context.section(SECTION)


def load(*, stale_after_days: int = 90, today: date | None = None) -> Strategy:
    """The loaded strategy, or Strategy.EMPTY when the section is absent —
    every consumer treats EMPTY as "necessity is neutral" (spec D3)."""
    return parse(section_text(), stale_after_days=stale_after_days, today=today)

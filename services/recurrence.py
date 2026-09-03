"""Completion-anchored task recurrence.

The rule lives in an Asana tag: `repeat:3mo` means "when this task is
completed, create the next occurrence due three months after the completion
date". Asana is the source of truth — there is no DB row, so a Postgres
outage cannot silently end a chain.

Design: docs/superpowers/specs/2026-09-03-recurring-tasks-design.md
"""

import logging
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

TAG_PREFIX = "repeat:"

# Bare "m" is deliberately absent: ambiguous between minutes and months, and
# guessing wrong schedules the next occurrence 30x early or late.
_UNITS = {
    "d": "days",
    "day": "days",
    "days": "days",
    "w": "weeks",
    "week": "weeks",
    "weeks": "weeks",
    "mo": "months",
    "mon": "months",
    "month": "months",
    "months": "months",
    "y": "years",
    "yr": "years",
    "year": "years",
    "years": "years",
}
_MAX_COUNT = 3650  # ~10 years; a larger number is a typo, not an intention
_RULE = re.compile(r"^\s*(\d+)\s*([a-z]+)\s*$")

# Asana timestamps are UTC. An 8pm ET completion is already tomorrow in UTC,
# which would date the successor a day late — so the date is taken locally.
# This is the repo's only timezone-aware code; it stays scoped to recurrence.
LOCAL_TZ = ZoneInfo("America/New_York")


def parse(tag_name: str) -> relativedelta | None:
    """`repeat:3mo` -> relativedelta(months=3).

    Returns None for anything that is not a well-formed rule, including tags
    that are not repeat tags at all. Never raises: a typo'd tag must not take
    down a completion event."""
    name = (tag_name or "").strip().casefold()
    if not name.startswith(TAG_PREFIX):
        return None
    match = _RULE.match(name[len(TAG_PREFIX) :])
    unit = _UNITS.get(match.group(2)) if match else None
    if match is None or unit is None:
        logger.warning("Unparseable repeat tag %r — ignoring", tag_name)
        return None
    count = int(match.group(1))
    if not 1 <= count <= _MAX_COUNT:
        logger.warning("Repeat tag %r has an out-of-range count — ignoring", tag_name)
        return None
    return relativedelta(**{unit: count})


def find_rule(tags: list[dict]) -> tuple[str, relativedelta] | None:
    """(tag_gid, interval) for a task's repeat tag, or None.

    None when there is no repeat tag, when the one present does not parse, or
    when there is more than one — guessing which of two rules was meant is
    worse than doing nothing and letting the tags be corrected."""
    candidates = [
        t for t in tags or [] if (t.get("name") or "").strip().casefold().startswith(TAG_PREFIX)
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "Task carries %d repeat tags (%s) — ignoring all",
            len(candidates),
            [t.get("name") for t in candidates],
        )
        return None
    interval = parse(candidates[0].get("name") or "")
    if interval is None:
        return None
    return candidates[0]["gid"], interval


def next_due(completed_at: str | None, interval: relativedelta) -> date:
    """Local completion date + interval.

    Falls back to today when Asana gave us no completed_at — a successor due
    on a slightly wrong day beats no successor at all."""
    if not completed_at:
        return date.today() + interval
    moment = datetime.fromisoformat(completed_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date() + interval

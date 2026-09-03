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

import clients.asana as asana
import clients.otel as otel
from services import sections, task_index

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
# Digit run is bounded so a pathological tag (thousands of digits) can't hit
# CPython's int() conversion limit and raise inside parse() — see the "Never
# raises" contract below. 6 digits comfortably covers _MAX_COUNT (4 digits).
_RULE = re.compile(r"^\s*(\d{1,6})\s*([a-z]+)\s*$")

# Asana timestamps are UTC. An 8pm ET completion is already tomorrow in UTC,
# which would date the successor a day late — so the date is taken locally.
# This is the repo's only timezone-aware code; it stays scoped to recurrence.
LOCAL_TZ = ZoneInfo("America/New_York")

# Successors carry external.gid = "recur:<completed gid>". This is the
# idempotency guard, and it is deliberately in Asana rather than Postgres:
# a webhook redelivery or an uncomplete/recomplete must not create a second
# occurrence even while the database is unreachable.
EXTERNAL_PREFIX = "recur:"


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

    Falls back to today (in local time, same as the timestamped path — a bare
    UTC `date.today()` would be tomorrow after 8pm ET) when Asana gave us no
    completed_at — a successor due on a slightly wrong day beats no successor
    at all."""
    if not completed_at:
        return datetime.now(LOCAL_TZ).date() + interval
    moment = datetime.fromisoformat(completed_at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date() + interval


def spawn_next(
    task: dict,
    detail: dict,
    section: dict | None,
    rule: tuple[str, relativedelta],
) -> str | None:
    """Create the successor to a just-completed recurring task.

    Returns the new gid, or None when a successor already exists. `section`
    is where the completed task lived *before* it was moved to Done — the
    successor goes back there."""
    tag_gid, interval = rule
    gid = task["gid"]
    external = f"{EXTERNAL_PREFIX}{gid}"

    existing = asana.find_task_by_external(external)
    if existing:
        logger.info("Recurrence for %s already created as %s — skipping", gid, existing)
        return None

    fields: dict = {
        "name": detail.get("name") or task.get("name") or "",
        "html_notes": detail.get("html_notes") or "<body></body>",
        "due_on": next_due(task.get("completed_at"), interval).isoformat(),
        "external": {"gid": external},
    }
    # get_task_detail can 404 (delete race) and hand back {}; get_task's tags
    # (already on `task`) are the fallback so the successor still carries the
    # repeat tag instead of silently dropping it and dead-ending the series.
    tag_gids = [t["gid"] for t in (detail.get("tags") or task.get("tags") or []) if t.get("gid")]
    if tag_gids:
        fields["tags"] = tag_gids
    assignee_gid = (detail.get("assignee") or {}).get("gid")
    if assignee_gid:
        fields["assignee"] = assignee_gid
    if asana.ASANA_PROJECT_ID:
        fields["projects"] = [asana.ASANA_PROJECT_ID]

    created = asana.create_task_from_fields(fields)
    otel.recurrences.add(1)
    logger.info("Recurring task %s → %s due %s", gid, created.gid, fields["due_on"])

    # The successor belongs where the last occurrence lived. If that was Done
    # (dragged there by hand), leave it unsectioned rather than filing a chore
    # under a mail-routing default.
    if section and section["gid"] != sections.done():
        _try(
            "place the successor in a section",
            asana.add_task_to_section,
            created.gid,
            section["gid"],
        )

    # Create-then-strip: the successor already exists, so a failed strip only
    # risks a duplicate on re-complete, which the external-gid guard catches.
    # The reverse order would risk a dead chain with nothing recorded.
    _try("strip the repeat tag", asana.remove_tag, gid, tag_gid)
    _try(
        "post the forward link",
        asana.create_story,
        gid,
        text=f"↻ Next occurrence: {created.permalink_url}",
    )
    task_index.refresh(created.gid)
    return created.gid


def _try(what: str, fn, *args, **kwargs) -> None:
    """Run a follow-up Asana call that must not cost us the successor."""
    try:
        fn(*args, **kwargs)
    except Exception:
        logger.exception("Recurrence: failed to %s", what)

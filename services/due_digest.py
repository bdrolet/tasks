"""Due-day digest — pure policy: which calendar a task belongs on, which tasks
fall in the window, how a day's tasks become one event, and the diff between
desired events and stored rows. No I/O; handlers/due_digest.py orchestrates.
Design: docs/superpowers/specs/2026-09-03-due-day-digest-design.md."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from models.digest import DigestEvent, DigestTask, Plan

PRIMARY = "primary"
WINDOW_DAYS = 30
LOCAL_TZ = "America/Los_Angeles"  # the primary calendar's zone (GET /calendars)

_PRIORITY_RE = re.compile(r"^\[P([0-3])\]")


def today_local(now: datetime | None = None) -> date:
    """The date in the primary calendar's zone — never the CF's UTC clock."""
    now = now or datetime.now(ZoneInfo(LOCAL_TZ))
    return now.astimezone(ZoneInfo(LOCAL_TZ)).date()


def route(task: dict, *, project_calendars: list[tuple[str, str]], shared_calendar_id: str) -> str:
    """First match wins: a project with a configured calendar, in the order the
    caller supplies; then a `cheryl` tag → shared calendar; else primary. A rule
    whose configuration is empty is skipped, so the digest still runs on a
    partial config. Never sorts — precedence is the caller's (handlers/due_digest
    ::_project_calendars, by the `order` key). Design:
    docs/superpowers/specs/2026-09-08-project-calendar-routing-design.md."""
    gids = {(m.get("project") or {}).get("gid") for m in task.get("memberships") or []}
    for project_gid, calendar_id in project_calendars:
        if project_gid and calendar_id and project_gid in gids:
            return calendar_id
    if shared_calendar_id:
        for tag in task.get("tags") or []:
            if (tag.get("name") or "").strip().casefold() == "cheryl":
                return shared_calendar_id
    return PRIMARY


def in_window(task: dict, today: date, days: int = WINDOW_DAYS) -> bool:
    """Open, dated, and due within [today, today + days] inclusive."""
    if task.get("completed"):
        return False
    due_on = task.get("due_on")
    if not due_on:
        return False
    due = date.fromisoformat(due_on)
    return today <= due <= today + timedelta(days=days)


def _priority_rank(name: str) -> int:
    m = _PRIORITY_RE.match(name or "")
    return int(m.group(1)) if m else 9


def order(tasks: list[DigestTask]) -> list[DigestTask]:
    return sorted(tasks, key=lambda t: (_priority_rank(t.name), t.name.casefold()))


def title_for(n: int) -> str:
    return f"{n} task due" if n == 1 else f"{n} tasks due"


def build_events(tasks: list[DigestTask]) -> dict[tuple[str, str], DigestEvent]:
    groups: dict[tuple[str, str], list[DigestTask]] = {}
    for task in tasks:
        groups.setdefault((task.due_on, task.calendar_id), []).append(task)
    events: dict[tuple[str, str], DigestEvent] = {}
    for key, members in groups.items():
        ordered = order(members)
        events[key] = DigestEvent(
            day=key[0],
            calendar_id=key[1],
            title=title_for(len(ordered)),
            sections=[
                {
                    "title": t.name,
                    "url": t.permalink_url,
                    "points": list(t.points),
                    "links": [[u, lbl] for u, lbl in t.links],
                }
                for t in ordered
            ],
            task_gids=[t.gid for t in ordered],
        )
    return events


def plan(desired: dict[tuple[str, str], DigestEvent], stored: list[dict], today: date) -> Plan:
    """Diff desired events against stored rows. Past days are never touched:
    the calendar keeps what was there. `day` in rows is ISO text (the repo
    normalizes it)."""
    result = Plan()
    by_key = {(str(r["day"]), r["calendar_id"]): r for r in stored}
    for key, event in desired.items():
        row = by_key.get(key)
        if row is None:
            result.creates.append(event)
        elif row["content_hash"] != event.content_hash():
            result.updates.append((event, row))
    today_iso = today.isoformat()
    for key, row in by_key.items():
        if key not in desired and key[0] >= today_iso:
            result.deletes.append(row)
    return result

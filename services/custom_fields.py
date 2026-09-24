"""The two custom fields the prioritizer owns, resolved by name (D1).

Field gids are workspace-scoped and stable, so one listing per process is
enough; `gids(refresh=True)` re-reads after the setup script creates them.
Values cross the Asana API as {field_gid: value} under `custom_fields`;
a number field takes a bare number, a date field takes {"date": "YYYY-MM-DD"}
or null."""

import logging
from datetime import date

import clients.asana as asana

logger = logging.getLogger(__name__)

STORY_POINTS = "Story points"
STARTED_AT = "Started at"
_SPECS: dict[str, tuple[str, int | None]] = {
    STORY_POINTS: ("number", 0),
    STARTED_AT: ("date", None),
}

_cache: dict[str, str] | None = None


def gids(refresh: bool = False) -> dict[str, str]:
    global _cache
    if _cache is None or refresh:
        _cache = {
            f["name"]: f["gid"] for f in asana.list_custom_fields() if f.get("name") in _SPECS
        }
    return dict(_cache)


def read(task: dict) -> tuple[int | None, date | None]:
    points: int | None = None
    started: date | None = None
    for f in task.get("custom_fields") or []:
        name = f.get("name")
        if name == STORY_POINTS and f.get("number_value") is not None:
            points = int(round(float(f["number_value"])))
        elif name == STARTED_AT and (f.get("date_value") or {}).get("date"):
            started = date.fromisoformat(f["date_value"]["date"])
    return points, started


def field_gid(name: str) -> str:
    """The field's gid, re-listing once on a miss; RuntimeError when absent."""
    gid = gids().get(name) or gids(refresh=True).get(name)
    if not gid:
        raise RuntimeError(f"custom field {name!r} missing — run scripts/setup_custom_fields.py")
    return gid


def date_value(day: date | str | None) -> dict | None:
    """Asana's wire shape for a date custom field: {"date": "YYYY-MM-DD"} or null to clear."""
    if day is None:
        return None
    return {"date": day.isoformat() if isinstance(day, date) else str(day)}


def set_story_points(task_gid: str, points: int | None) -> None:
    asana.update_task(task_gid, {"custom_fields": {field_gid(STORY_POINTS): points}})


def set_started_at(task_gid: str, day: date | None) -> None:
    asana.update_task(task_gid, {"custom_fields": {field_gid(STARTED_AT): date_value(day)}})


def ensure(project_gids: list[str]) -> dict[str, str]:
    """Create the fields that are missing and attach both to every project.
    Attaching an already-attached field is an Asana no-op error we ignore."""
    existing = {f["name"]: f for f in asana.list_custom_fields()}
    out: dict[str, str] = {}
    for name, (subtype, precision) in _SPECS.items():
        field = existing.get(name) or asana.create_custom_field(name, subtype, precision=precision)
        out[name] = field["gid"]
    for project_gid in project_gids:
        for name, gid in out.items():
            try:
                asana.add_custom_field_to_project(project_gid, gid)
            except Exception:  # noqa: BLE001 — already attached is a 4xx we do not need to read
                logger.info(
                    "field %s already on project %s (or attach failed) — continuing",
                    name,
                    project_gid,
                )
    gids(refresh=True)
    return out

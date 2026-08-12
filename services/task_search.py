"""Workspace task search — pure filtering/resolution logic, no I/O.

The Asana free tier has no full-text search endpoint (402), so the search
router lists tasks (per project or workspace-wide) and this module filters
them: case-insensitive substring match on name + notes, completion/due-date
filters, de-dupe (my-tasks listing overlaps project listings), stable sort.
"""

import re


def resolve_project(projects: list[dict], ref: str) -> dict | None:
    """Match a project by exact GID, else case-insensitive name."""
    for p in projects:
        if p["gid"] == ref:
            return p
    folded = ref.casefold()
    for p in projects:
        if (p.get("name") or "").casefold() == folded:
            return p
    return None


def _matches(task: dict, query: str) -> bool:
    q = query.casefold()
    return q in (task.get("name") or "").casefold() or q in (task.get("notes") or "").casefold()


# The description standard (services/task_content.py) renders every task the
# same way: optional lead prose, then these headers. Matching them is how the
# gist is separated from the boilerplate below it. "Actions" carries no colon,
# so it needs the run of list indent to avoid eating prose that opens with the
# word.
_SECTION_RE = re.compile(r"^(?:Key points:|Links:|Source:)(?:\s|$)|^Actions(?:\s{2,}|$)")


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return head.rstrip(" ,;:.") + "…"


def summary(notes: str | None, limit: int = 160) -> str | None:
    """A one-line gist of a task's description, for listings.

    Prefers the lead context prose the description standard puts above the
    section headers, falling back to the first key point when a task leads
    with the bullets instead (email-derived tasks usually do). Deterministic,
    never an LLM — the same notes always yield the same line.
    """
    if not notes:
        return None
    lines = notes.split("\n")

    lead = []
    for line in lines:
        if _SECTION_RE.match(line.strip()):
            break
        if line.strip():
            lead.append(line.strip())
    if lead:
        return _clip(" ".join(lead), limit)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("Key points:"):
            continue
        first = stripped[len("Key points:") :].strip()
        if not first:
            first = next((n.strip() for n in lines[i + 1 :] if n.strip()), "")
        return _clip(first, limit) or None
    return None


def snippet(notes: str | None, query: str, radius: int = 60) -> str | None:
    """The matched fragment of notes with ± radius chars of context."""
    if not notes or not query:
        return None
    idx = notes.casefold().find(query.casefold())
    if idx == -1:
        return None
    start, end = max(0, idx - radius), min(len(notes), idx + len(query) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(notes) else ""
    return f"{prefix}{notes[start:end]}{suffix}"


def filter_tasks(
    tasks: list[dict],
    *,
    query: str,
    completed: bool | None,
    due_before: str | None,
    due_after: str | None,
) -> list[dict]:
    """De-dupe by gid, apply filters (date bounds inclusive; date filters drop
    undated tasks), sort by due date ascending with undated last, then name."""
    seen: set[str] = set()
    out: list[dict] = []
    for t in tasks:
        if t["gid"] in seen:
            continue
        seen.add(t["gid"])
        if completed is not None and bool(t.get("completed")) != completed:
            continue
        if due_before or due_after:
            due_on = t.get("due_on")
            if not due_on:
                continue
            if due_before and due_on > due_before:
                continue
            if due_after and due_on < due_after:
                continue
        if query and not _matches(t, query):
            continue
        out.append(t)
    return sorted(out, key=lambda t: (t.get("due_on") or "9999-12-31", t.get("name") or ""))

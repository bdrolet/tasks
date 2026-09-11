"""Pure reconciliation diff: managed projects vs. registered webhooks.

No Asana, no database — handlers/webhook_sync.py does the I/O against this.

Design: docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md (D5)
"""

from typing import NamedTuple
from urllib.parse import parse_qs, urlparse


class Plan(NamedTuple):
    to_register: list[str]  # project gids
    to_delete: list[tuple[str, str]]  # (project gid, webhook gid)


def _normalized_path(path: str) -> str:
    """A trailing slash is insignificant; an empty path is the same as `/`."""
    return path.rstrip("/") or "/"


def target_project(target: str, base_url: str) -> str | None:
    """The `project` query parameter of one of our webhook targets.

    None for a target that is not ours, or that carries no project — which is
    exactly the legacy single-project webhook (D7). Returning None there is
    what keeps reconciliation from deleting it during the rollout.

    Scheme, host and path must all match `base_url` (scheme/host compared
    case-insensitively; path with a trailing slash treated as insignificant).
    This service runs two Cloud Functions on the same host — `tasks-events`
    and `tasks-webhook` — differing only by path, so a host-only match would
    misidentify a sibling function's webhook as ours and queue it for
    deletion. Fail closed on anything ambiguous, including a mismatched
    port (carried in netloc)."""
    if not target:
        return None
    parsed, base = urlparse(target), urlparse(base_url)
    if parsed.scheme.lower() != base.scheme.lower():
        return None
    if parsed.netloc.lower() != base.netloc.lower():
        return None
    if _normalized_path(parsed.path) != _normalized_path(base.path):
        return None
    return (parse_qs(parsed.query).get("project") or [None])[0]


def plan(managed: set[str], registered: dict[str, str], with_secrets: set[str]) -> Plan:
    """What to change so every managed project has exactly one live webhook
    whose secret we hold.

    `registered` is {project gid: webhook gid} for our own project-scoped
    webhooks; `with_secrets` is the set of projects with an asana_webhooks row.

    Deletes must be applied before registrations: a project whose webhook has
    no secret row appears in both lists, and the replacement only works in
    that order."""
    to_register: list[str] = []
    to_delete: list[tuple[str, str]] = []

    for gid in sorted(managed):
        webhook_gid = registered.get(gid)
        if webhook_gid is None:
            to_register.append(gid)
        elif gid not in with_secrets:
            # Half-finished registration: the webhook exists but we cannot
            # validate anything it delivers. Replace it rather than leave a
            # project silently dead — the failure this whole spec is about.
            to_delete.append((gid, webhook_gid))
            to_register.append(gid)

    for gid, webhook_gid in sorted(registered.items()):
        if gid not in managed:
            to_delete.append((gid, webhook_gid))

    return Plan(to_register, to_delete)

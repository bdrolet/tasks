"""Pure reconciliation diff: managed projects vs. registered webhooks, and
the shape of the target URL we register.

No Asana, no database — handlers/webhook_sync.py does the I/O against this.

Design: docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md (D5)
"""

import hashlib
import hmac
import logging
import os
from collections.abc import Collection
from typing import NamedTuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# The target URL is the only thing that tells the handshake which project is
# registering, and the webhook CF is publicly invokable — so the target has to
# be unforgeable or anyone could mint a secret row for any project gid (and
# project gids leak through every app.asana.com/0/<project>/<task> permalink).
# We key the token off ASANA_ESCALATE_TOKEN, the bearer the webhook function
# already holds for /escalate, /digest and /webhook-sync; only Asana — which
# receives the target from us — and the reconciler ever see it.
_SIGNING_KEY_ENV = "ASANA_ESCALATE_TOKEN"
TOKEN_PARAM = "t"


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
    projects = parse_qs(parsed.query).get("project") or []
    return projects[0] if projects else None


def _signing_key() -> str:
    key = os.environ.get(_SIGNING_KEY_ENV, "")
    if not key:
        raise RuntimeError(f"{_SIGNING_KEY_ENV} is not set — project targets cannot be signed")
    return key


def project_token(project_gid: str) -> str:
    """The `t` parameter for a project's target URL.

    One function, used by both the reconciler that builds the target and the
    handshake that verifies it, so the two cannot drift. Raises when the
    signing key is unset: registering a target we could never verify would
    leave the project permanently un-handshakeable."""
    return hmac.new(_signing_key().encode(), project_gid.encode(), hashlib.sha256).hexdigest()


def token_valid(project_gid: str, token: str | None) -> bool:
    """Constant-time check of a handshake's `t` against project_token()."""
    if not token:
        return False
    try:
        expected = project_token(project_gid)
    except RuntimeError:
        logger.error("%s is not set — cannot verify a project handshake token", _SIGNING_KEY_ENV)
        return False
    return hmac.compare_digest(expected, token)


def target_for(base_url: str, project_gid: str) -> str:
    """The target we register with Asana: the project gid plus its token."""
    return f"{base_url}?project={project_gid}&{TOKEN_PARAM}={project_token(project_gid)}"


def plan(
    managed: set[str],
    registered: dict[str, str],
    with_secrets: set[str],
    inactive: Collection[str] = (),
) -> Plan:
    """What to change so every managed project has exactly one live, healthy
    webhook whose secret we hold.

    `registered` is {project gid: webhook gid} for our own project-scoped
    webhooks; `with_secrets` is the set of projects with an asana_webhooks row;
    `inactive` is the set of projects whose webhook Asana has marked
    `active: false` — it still exists and still parses, but it is delivering
    nothing.

    Deletes must be applied before registrations: a project whose webhook is
    unusable appears in both lists, and the replacement only works in that
    order."""
    to_register: list[str] = []
    to_delete: list[tuple[str, str]] = []

    for gid in sorted(managed):
        webhook_gid = registered.get(gid)
        if webhook_gid is None:
            to_register.append(gid)
        elif gid not in with_secrets or gid in inactive:
            # Unusable, two ways. No secret row: a half-finished registration
            # whose deliveries we could never validate. active: false: Asana
            # disabled it after repeated delivery failures and will drop it
            # entirely at 24 hours. Either way, replace rather than leave the
            # project silently dead — the failure this whole spec is about.
            to_delete.append((gid, webhook_gid))
            to_register.append(gid)

    for gid, webhook_gid in sorted(registered.items()):
        if gid not in managed:
            to_delete.append((gid, webhook_gid))

    return Plan(to_register, to_delete)

"""Asana webhook protocol: handshake echo, HMAC signature validation, and
event dispatch. main.py owns transport (routing, flush); this module owns
everything about the webhook payload — new Asana event types get handled
here, never in main.py."""

import hashlib
import hmac
import json
import logging
import os
import time

from clients.db import get_conn
import clients.otel as otel
from handlers import task_complete
from repo import asana_webhooks as repo_webhooks
from repo import due_digest as repo_due_digest
from services import managed_projects, task_index

logger = logging.getLogger(__name__)

_MAX_REFRESH_PER_DELIVERY = 20

# Steady state does no database read on the delivery path. Correctness never
# depends on the cache: a miss falls through to Postgres, and a miss during a
# DB outage rejects, which Asana retries (D6).
_SECRET_TTL_SECONDS = 600
_secret_cache: dict[str, tuple[float, str]] = {}


def _now() -> float:
    return time.monotonic()


def _secret_for(project_gid: str) -> str | None:
    cached = _secret_cache.get(project_gid)
    if cached and _now() - cached[0] < _SECRET_TTL_SECONDS:
        return cached[1]
    try:
        with get_conn() as conn:
            secret = repo_webhooks.get_secret(conn, project_gid)
    except Exception:
        # The one place a DB outage may cost a delivery. Safe only because
        # rejecting is non-destructive here — Asana redelivers for 24 hours.
        logger.warning(
            "Webhook secret lookup failed for project %s — rejecting, Asana will retry",
            project_gid,
            exc_info=True,
        )
        return None
    if secret:
        _secret_cache[project_gid] = (_now(), secret)
    return secret


def handshake(hook_secret: str, project_gid: str | None = None) -> tuple:
    """Echo X-Hook-Secret, storing it against the project that is registering.

    Without a project this is the legacy single-webhook path (D7): the secret
    is logged so the runbook can put it in Secret Manager by hand."""
    if not project_gid:
        logger.info("Asana webhook handshake — X-Hook-Secret: %s", hook_secret)
        return "", 200, {"X-Hook-Secret": hook_secret}
    try:
        with get_conn() as conn:
            repo_webhooks.upsert_secret(conn, project_gid, hook_secret)
    except Exception:
        # Fail the handshake rather than echo: Asana's create call then fails
        # and no webhook exists whose deliveries we could never validate.
        logger.exception("Webhook handshake: storing the secret for %s failed", project_gid)
        return "", 500
    _secret_cache[project_gid] = (_now(), hook_secret)
    logger.info("Asana webhook handshake stored for project %s", project_gid)
    return "", 200, {"X-Hook-Secret": hook_secret}


def signature_valid(body: bytes, signature: str, project_gid: str | None = None) -> bool:
    if project_gid:
        secret = _secret_for(project_gid)
        if not secret:
            reason = "no_secret" if project_gid in managed_projects.gids() else "unknown_project"
            otel.webhook_auth_failures.add(1, {"reason": reason})
            logger.warning("No webhook secret for project %s (%s)", project_gid, reason)
            return False
    else:
        secret = os.environ.get("ASANA_WEBHOOK_SECRET", "")
        if not secret:
            otel.webhook_auth_failures.add(1, {"reason": "no_secret"})
            logger.warning("ASANA_WEBHOOK_SECRET not set — rejecting webhook event")
            return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        otel.webhook_auth_failures.add(1, {"reason": "bad_signature"})
        return False
    return True


def _mark_digest_dirty() -> None:
    """Best-effort: the 10-minute /digest tick also rebuilds hourly, so a
    lost flag delays the digest, never the webhook."""
    try:
        with get_conn() as conn:
            repo_due_digest.mark_dirty(conn)
    except Exception:
        logger.warning(
            "Digest dirty flag write failed — hourly rebuild will catch up", exc_info=True
        )


def receive(body: bytes, signature: str, project_gid: str | None = None) -> tuple:
    """Validate and dispatch one webhook delivery.

    `project_gid` comes from the target's ?project= parameter and selects the
    secret; dispatch itself is project-agnostic, because each task carries its
    own memberships."""
    if not signature_valid(body, signature, project_gid):
        logger.warning("Invalid webhook signature — rejecting")
        return "", 401

    payload = json.loads(body or b"{}")
    handled = 0
    refresh_gids: dict[str, None] = {}  # insertion-ordered de-dupe
    delete_gids: dict[str, None] = {}  # insertion-ordered de-dupe
    digest_relevant = False
    for event in payload.get("events", []):
        resource = event.get("resource") or {}
        if resource.get("resource_type") != "task":
            continue
        action = event.get("action")
        field = (event.get("change") or {}).get("field")
        if action == "changed" and field == "completed":
            task_complete.handle(resource["gid"])
            handled += 1
            digest_relevant = True
        elif action in ("deleted", "removed"):
            delete_gids[resource["gid"]] = None
            digest_relevant = True
        elif action == "added" or (action == "changed" and field in ("name", "notes", "due_on")):
            refresh_gids[resource["gid"]] = None
            digest_relevant = True

    # delete wins: a gid deleted in this delivery is never also refreshed
    for gid in delete_gids:
        refresh_gids.pop(gid, None)

    if digest_relevant:
        _mark_digest_dirty()

    refresh_list = list(refresh_gids)
    if len(refresh_list) > _MAX_REFRESH_PER_DELIVERY:
        logger.warning(
            "Webhook: %d refresh gids exceeds cap %d — remainder heals via backfill",
            len(refresh_list),
            _MAX_REFRESH_PER_DELIVERY,
        )
        refresh_list = refresh_list[:_MAX_REFRESH_PER_DELIVERY]
    for gid in refresh_list:
        task_index.refresh(gid)
    for gid in delete_gids:
        task_index.remove(gid)

    logger.info(
        "Webhook: %d event(s) received, %d completion(s), %d index refresh(es), "
        "%d index delete(s) — signature_valid: true",
        len(payload.get("events", [])),
        handled,
        len(refresh_list),
        len(delete_gids),
    )
    return "", 200

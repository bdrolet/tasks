"""Asana webhook protocol: handshake echo, HMAC signature validation, and
event dispatch. main.py owns transport (routing, flush); this module owns
everything about the webhook payload — new Asana event types get handled
here, never in main.py."""

import hashlib
import hmac
import json
import logging
import os

from clients.db import get_conn
from handlers import task_complete
from repo import due_digest as repo_due_digest
from services import task_index

logger = logging.getLogger(__name__)

_MAX_REFRESH_PER_DELIVERY = 20


def handshake(hook_secret: str) -> tuple:
    """Echo X-Hook-Secret. Logged so the runbook can store it in Secret
    Manager (docs/asana-webhook-setup.md)."""
    logger.info("Asana webhook handshake — X-Hook-Secret: %s", hook_secret)
    return "", 200, {"X-Hook-Secret": hook_secret}


def signature_valid(body: bytes, signature: str) -> bool:
    secret = os.environ.get("ASANA_WEBHOOK_SECRET", "")
    if not secret:
        logger.warning("ASANA_WEBHOOK_SECRET not set — rejecting webhook event")
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


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


def receive(body: bytes, signature: str) -> tuple:
    """Validate and dispatch one webhook delivery."""
    if not signature_valid(body, signature):
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

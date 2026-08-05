"""Asana webhook protocol: handshake echo, HMAC signature validation, and
event dispatch. main.py owns transport (routing, flush); this module owns
everything about the webhook payload — new Asana event types get handled
here, never in main.py."""

import hashlib
import hmac
import json
import logging
import os

from handlers import task_complete
from services import task_index

logger = logging.getLogger(__name__)


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


def receive(body: bytes, signature: str) -> tuple:
    """Validate and dispatch one webhook delivery."""
    if not signature_valid(body, signature):
        logger.warning("Invalid webhook signature — rejecting")
        return "", 401

    payload = json.loads(body or b"{}")
    handled = 0
    refresh_gids: dict[str, None] = {}  # insertion-ordered de-dupe
    for event in payload.get("events", []):
        resource = event.get("resource") or {}
        if resource.get("resource_type") != "task":
            continue
        action = event.get("action")
        field = (event.get("change") or {}).get("field")
        if action == "changed" and field == "completed":
            task_complete.handle(resource["gid"])
            handled += 1
        elif action == "added" or (action == "changed" and field in ("name", "notes", "due_on")):
            refresh_gids[resource["gid"]] = None
    for gid in refresh_gids:
        task_index.refresh(gid)
    logger.info(
        "Webhook: %d event(s) received, %d completion(s), %d index refresh(es) — signature_valid: true",
        len(payload.get("events", [])),
        handled,
        len(refresh_gids),
    )
    return "", 200

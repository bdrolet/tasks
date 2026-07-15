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
    for event in payload.get("events", []):
        if event.get("action") == "changed" and event.get("change", {}).get("field") == "completed":
            task_complete.handle(event["resource"]["gid"])
            handled += 1
    logger.info(
        "Webhook: %d event(s) received, %d completion(s) handled — signature_valid: true",
        len(payload.get("events", [])),
        handled,
    )
    return "", 200

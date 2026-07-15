"""Client for the inbox-api Cloud Run service — the mailbox gateway.

This repo never talks to Microsoft Graph directly (no MSAL here, by design:
a second writer to the shared MSAL token cache risks refresh-token
clobbering). Anything mailbox-shaped goes through inbox-api's bearer-authed
HTTP interface. No pipeline consumer yet — this is the seam for post-creation
task actions (attachments-on-task, re-summarize, reply drafts)."""

import os

import httpx

INBOX_API_URL = os.environ.get("INBOX_API_URL", "")
INBOX_API_TOKEN = os.environ.get("INBOX_API_TOKEN", "")


def _get(path: str) -> dict:
    resp = httpx.get(
        f"{INBOX_API_URL}{path}",
        headers={"Authorization": f"Bearer {INBOX_API_TOKEN}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def get_email(message_id: str) -> dict:
    """Full email detail (body, recipients) — GET /emails/{message_id}."""
    return _get(f"/emails/{message_id}")


def get_attachments(message_id: str) -> dict:
    """Attachment list with content — GET /emails/{message_id}/attachments."""
    return _get(f"/emails/{message_id}/attachments")

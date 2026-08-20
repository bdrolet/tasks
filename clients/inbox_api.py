"""Client for the inbox-api Cloud Run service — the mailbox gateway.

This repo never talks to Microsoft Graph directly (no MSAL here, by design:
a second writer to the shared MSAL token cache risks refresh-token
clobbering). Anything mailbox-shaped goes through inbox-api's bearer-authed
HTTP interface. First pipeline consumer: the triage agent's `search_emails` /
`get_email` tools (services/triage.py)."""

import os

import httpx

INBOX_API_URL = os.environ.get("INBOX_API_URL", "")
INBOX_API_TOKEN = os.environ.get("INBOX_API_TOKEN", "")

# Graph search fans out across the primary mailbox plus several shared
# mailboxes and M365 groups; 10s was observed to time out against real mail,
# and a timed-out search silently costs the triage agent its evidence.
SEARCH_TIMEOUT = 20


def _get(path: str) -> dict:
    resp = httpx.get(
        f"{INBOX_API_URL}{path}",
        headers={"Authorization": f"Bearer {INBOX_API_TOKEN}"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def get_email(message_id: str) -> dict:
    """Full email detail (body, recipients) — GET /emails/{message_id}."""
    return _get(f"/emails/{message_id}")


def get_attachments(message_id: str) -> dict:
    """Attachment list with content — GET /emails/{message_id}/attachments."""
    return _get(f"/emails/{message_id}/attachments")


def search(
    query: str, *, mode: str = "graph", limit: int = 10, mailboxes: list[str] | None = None
) -> list[dict]:
    """POST /search — `graph` mode is live Outlook KQL (`from:`, `subject:`,
    keywords) across the primary + shared mailboxes; `db` mode is processed
    mail with category/importance. Returns the `results` list."""
    payload: dict = {"query": query, "mode": mode, "limit": limit}
    if mailboxes:
        payload["mailboxes"] = mailboxes
    resp = httpx.post(
        f"{INBOX_API_URL}/search",
        json=payload,
        headers={"Authorization": f"Bearer {INBOX_API_TOKEN}"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])

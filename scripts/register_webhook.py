"""One-time Asana webhook registration for the tasks-webhook CF.

Usage:
    .venv/bin/python scripts/register_webhook.py <tasks-webhook-cf-url>

Reads ASANA_API_KEY + ASANA_PROJECT_ID from .env / environment. Registers a
webhook filtered to task "completed" changes. Asana sends the X-Hook-Secret to
the CF (not here) during the handshake — the CF logs it; see the printed
instructions for storing it.
"""

import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: register_webhook.py <tasks-webhook-cf-url>")
    target = sys.argv[1]
    api_key = os.environ["ASANA_API_KEY"]
    project = os.environ["ASANA_PROJECT_ID"]

    resp = httpx.post(
        "https://app.asana.com/api/1.0/webhooks",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "data": {
                "resource": project,
                "target": target,
                "filters": [
                    {"resource_type": "task", "action": "changed", "fields": ["completed"]}
                ],
            }
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()["data"]
    print(f"Webhook registered: gid={data['gid']} active={data['active']}")
    print()
    print("X-Hook-Secret was delivered to the CF during the handshake. Fetch it with:")
    print(
        "  gcloud functions logs read tasks-webhook --project=bens-project-462804 "
        "--region=us-central1 --limit=20 | grep 'X-Hook-Secret'"
    )
    print("Then follow docs/asana-webhook-setup.md to store it (tfvars + GH secret + apply).")


if __name__ == "__main__":
    main()

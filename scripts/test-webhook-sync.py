#!/usr/bin/env python3
"""Show what the webhook reconciler would do, without changing anything.

  .venv/bin/python scripts/test-webhook-sync.py --target <tasks-webhook-cf-url>

Reads ASANA_API_KEY, ASANA_MANAGED_PROJECTS and the Postgres vars from .env
(scripts/fetch-env.sh). Read-only: it lists webhooks and secret rows and
prints the diff. It never imports create_webhook, delete_webhook,
set_webhook_gid or repo.asana_webhooks.delete — nothing it calls can write to
Asana or the database. To actually reconcile, POST /webhook-sync on the
deployed CF.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

import clients.asana as asana
from clients.db import get_conn
from repo import asana_webhooks as repo_webhooks
from services import managed_projects, webhook_registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="tasks-webhook CF base URL")
    args = parser.parse_args()

    managed = managed_projects.gids()
    print(f"Managed projects ({len(managed)}): {', '.join(sorted(managed)) or '(none)'}")

    registered = {}
    for hook in asana.list_webhooks():
        project_gid = webhook_registry.target_project(hook.get("target") or "", args.target)
        if project_gid:
            registered[project_gid] = hook["gid"]
        else:
            print(f"  ignoring webhook {hook['gid']} — target {hook.get('target')!r}")
    print(f"Registered for us ({len(registered)}): {registered or '(none)'}")

    with get_conn() as conn:
        with_secrets = {row["project_gid"] for row in repo_webhooks.list_all(conn)}
    print(f"Secret rows ({len(with_secrets)}): {', '.join(sorted(with_secrets)) or '(none)'}")

    plan = webhook_registry.plan(managed, registered, with_secrets)
    print(f"\nWould delete: {plan.to_delete or '(nothing)'}")
    print(f"Would register: {plan.to_register or '(nothing)'}")


if __name__ == "__main__":
    main()

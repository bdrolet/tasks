#!/usr/bin/env python3
"""Show what the webhook reconciler would do, without changing anything.

  .venv/bin/python scripts/test-webhook-sync.py --target <tasks-webhook-cf-url>

Reads ASANA_API_KEY, ASANA_MANAGED_PROJECTS and the Postgres vars from .env
(scripts/fetch-env.sh). Read-only: the only Asana and database functions in
scope are `clients.asana.list_webhooks` and `repo.asana_webhooks.list_all` —
both reads. `services.webhook_registry` and `services.managed_projects` do
no I/O at all. To actually reconcile, POST /webhook-sync on the deployed CF.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

from clients.asana import list_webhooks
from clients.db import get_conn
from repo.asana_webhooks import list_all
from services import managed_projects, webhook_registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, help="tasks-webhook CF base URL")
    args = parser.parse_args()

    managed = managed_projects.gids()
    print(f"Managed projects ({len(managed)}): {', '.join(sorted(managed)) or '(none)'}")

    registered = {}
    inactive = set()
    for hook in list_webhooks():
        project_gid = webhook_registry.target_project(hook.get("target") or "", args.target)
        if not project_gid:
            print(f"  ignoring webhook {hook['gid']} — target {hook.get('target')!r}")
            continue
        resource_gid = (hook.get("resource") or {}).get("gid")
        if resource_gid and resource_gid != project_gid:
            print(
                f"  SKIPPING webhook {hook['gid']} — target says project {project_gid}, "
                f"resource says {resource_gid}"
            )
            continue
        registered[project_gid] = hook["gid"]
        if hook.get("active") is False:
            inactive.add(project_gid)
            print(f"  webhook {hook['gid']} for project {project_gid} is INACTIVE")
    print(f"Registered for us ({len(registered)}): {registered or '(none)'}")

    with get_conn() as conn:
        with_secrets = {row["project_gid"] for row in list_all(conn)}
    print(f"Secret rows ({len(with_secrets)}): {', '.join(sorted(with_secrets)) or '(none)'}")

    plan = webhook_registry.plan(managed, registered, with_secrets, inactive)
    if not managed and plan.to_delete:
        print(
            f"\nWould REFUSE to delete {len(plan.to_delete)} webhook(s): "
            f"{managed_projects.ENV_VAR} is empty (safety valve in handlers/webhook_sync.py)"
        )
        return
    print(f"\nWould delete: {plan.to_delete or '(nothing)'}")
    print(f"Would register: {plan.to_register or '(nothing)'}")


if __name__ == "__main__":
    main()

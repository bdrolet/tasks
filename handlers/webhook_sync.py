"""Reconcile Asana webhook registrations against the managed-project map.

Asana deletes a webhook after 24 hours of failed delivery and mints a fresh
X-Hook-Secret for every new one, so registration has to be a repairable
steady state rather than a runbook step. Cloud Scheduler drives this daily
(tasks-webhook-sync → POST /webhook-sync).

Unlike the delivery path, this handler lets a database failure raise: a run
that cannot read its own secret rows would compute a diff that deletes and
re-registers everything. Failing is correct here — the next tick retries.

Design: docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md (D5)
"""

import logging

import clients.asana as asana
import clients.otel as otel
from clients.db import get_conn
from repo import asana_webhooks as repo_webhooks
from services import managed_projects, webhook_registry

logger = logging.getLogger(__name__)


def run(target_url: str) -> dict:
    """Bring registrations in line with ASANA_MANAGED_PROJECTS."""
    managed = managed_projects.gids()

    registered: dict[str, str] = {}
    for hook in asana.list_webhooks():
        project_gid = webhook_registry.target_project(hook.get("target") or "", target_url)
        if project_gid:
            registered[project_gid] = hook["gid"]

    with get_conn() as conn:
        with_secrets = {row["project_gid"] for row in repo_webhooks.list_all(conn)}

    plan = webhook_registry.plan(managed, registered, with_secrets)
    live = {gid for gid in registered if gid in managed}

    # Deletes first: a project being replaced (webhook with no secret row)
    # appears in both lists and only reconciles in that order.
    deleted = 0
    for project_gid, webhook_gid in plan.to_delete:
        try:
            asana.delete_webhook(webhook_gid)
        except Exception:
            logger.exception(
                "Webhook sync: deleting %s for project %s failed", webhook_gid, project_gid
            )
            continue
        deleted += 1
        live.discard(project_gid)
        if project_gid not in managed:
            with get_conn() as conn:
                repo_webhooks.delete(conn, project_gid)

    registered_count = 0
    for project_gid in plan.to_register:
        try:
            # Asana calls back into handshake() during this POST; that is what
            # writes the secret row keyed on the ?project= parameter (D4).
            hook = asana.create_webhook(
                project_gid, webhook_registry.target_for(target_url, project_gid)
            )
            with get_conn() as conn:
                repo_webhooks.set_webhook_gid(conn, project_gid, hook["gid"])
        except Exception:
            logger.exception(
                "Webhook sync: registering project %s failed — retrying next tick", project_gid
            )
            continue
        registered_count += 1
        live.add(project_gid)

    otel.webhooks_registered.add(registered_count)
    otel.webhooks_deleted.add(deleted)
    otel.webhooks_active.set(len(live))
    logger.info(
        "Webhook sync: %d managed, %d registered, %d deleted, %d active",
        len(managed),
        registered_count,
        deleted,
        len(live),
    )
    return {
        "managed": len(managed),
        "registered": registered_count,
        "deleted": deleted,
        "active": len(live),
    }

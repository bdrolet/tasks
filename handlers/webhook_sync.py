"""Reconcile Asana webhook registrations against the managed-project map.

Asana deletes a webhook after 24 hours of failed delivery and mints a fresh
X-Hook-Secret for every new one, so registration has to be a repairable
steady state rather than a runbook step. Cloud Scheduler drives this daily
(tasks-webhook-sync → POST /webhook-sync).

Unlike the delivery path, this handler lets a database failure raise: a run
that cannot read its own secret rows would compute a diff that deletes and
re-registers everything. Failing is correct here — the next tick retries.

For the same reason an empty managed map never deletes anything: see the
safety valve in run(). A destructive diff must not be driven by an absent
config value.

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
    inactive: set[str] = set()
    for hook in asana.list_webhooks():
        project_gid = webhook_registry.target_project(hook.get("target") or "", target_url)
        if not project_gid:
            continue
        # A target claiming one project while the webhook is registered on
        # another resource is a real inconsistency, and acting on it would
        # mean deleting or counting the wrong thing. Skip and say so.
        resource_gid = (hook.get("resource") or {}).get("gid")
        if resource_gid and resource_gid != project_gid:
            logger.error(
                "Webhook sync: webhook %s targets project %s but is registered on resource %s "
                "— skipping, resolve by hand",
                hook["gid"],
                project_gid,
                resource_gid,
            )
            continue
        registered[project_gid] = hook["gid"]
        # Asana sets active: false on a webhook whose deliveries keep failing.
        # It still exists and its target still parses, so counting it live
        # would let asana.webhooks.active read healthy while nothing is being
        # delivered — and that gauge is the alert that matters.
        if hook.get("active") is False:
            inactive.add(project_gid)
            logger.warning(
                "Webhook sync: webhook %s for project %s is inactive — replacing",
                hook["gid"],
                project_gid,
            )

    with get_conn() as conn:
        with_secrets = {row["project_gid"] for row in repo_webhooks.list_all(conn)}

    plan = webhook_registry.plan(managed, registered, with_secrets, inactive)
    live = {gid for gid in registered if gid in managed and gid not in inactive}

    # Safety valve. managed_projects.managed() degrades an unset, blank or
    # malformed map to {} — right for sections.done(), wrong for a destructive
    # diff, where it puts every project-scoped webhook into to_delete and ends
    # recurrence everywhere. It is reachable by config slip, not only by
    # intent: .github/workflows/deploy.yml passes an undefined repo variable
    # through as "", which sets the Terraform variable to empty and overrides
    # variables.tf's default. Deregistering the last managed project should
    # take one deliberate act — empty the map AND delete the webhooks by hand
    # — rather than a missing variable. Not an exception: the scheduler tick
    # should not look like an outage. The gauge still goes out, and reads 0,
    # which is exactly the alert the spec's Observability section wants.
    if not managed and plan.to_delete:
        otel.webhooks_active.set(len(live))
        logger.error(
            "Webhook sync: %s is empty but %d project webhook(s) are registered (%s) — "
            "refusing to delete them. Set %s, or deregister deliberately.",
            managed_projects.ENV_VAR,
            len(plan.to_delete),
            ", ".join(gid for gid, _ in plan.to_delete),
            managed_projects.ENV_VAR,
        )
        return {
            "managed": 0,
            "registered": 0,
            "deleted": 0,
            "active": len(live),
            "refused_deletes": len(plan.to_delete),
        }

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

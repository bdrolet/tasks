"""
Cloud Function entry points for the tasks service.

process — Pub/Sub trigger on the inbox-owned email-events topic; handles
          email_classified (policy → enrich → create task) and label_applied.
webhook — HTTP trigger (public); Asana webhook handshake + completion events,
          and POST /escalate for the Cloud Scheduler overdue scan.

LAYER RULE: this file is a transport adapter — decode the envelope, route,
flush telemetry, count errors. All behavior lives in handlers/ and services/;
main.py grows only when a new trigger is added.

Both deploy from this repo root with different entry points (same pattern as
inbox's process/sweep). Required env vars:
  ASANA_API_KEY / ASANA_PROJECT_ID           — Asana REST auth + target project
  ANTHROPIC_API_KEY                          — enrichment (summary, deadline)
  ASANA_SECTION_{REVIEW,RESPOND,URGENT,DONE,OVERDUE}_GID — section mapping
  ASANA_WEBHOOK_SECRET                       — HMAC key for X-Hook-Signature (webhook CF)
  ASANA_ESCALATE_TOKEN                       — bearer token for POST /escalate (webhook CF)
  WEBHOOK_URL / WEBHOOK_LABEL_TOKEN          — inbox webhook CF, for task action links
  CLOUD_SQL_CONNECTION_NAME / POSTGRES_*     — tasks database
  GRAFANA_OTLP_ENDPOINT / GRAFANA_OTLP_TOKEN — OTel export (optional)
"""

import base64
import json
import logging
import os

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

import functions_framework
from cloudevents.http import CloudEvent

import clients.otel as otel
from handlers import asana_webhook, label_applied, task_create
from services import escalation

logger = logging.getLogger(__name__)

otel.setup_telemetry(os.environ.get("K_SERVICE", "tasks-local"))


@functions_framework.cloud_event
def process(cloud_event: CloudEvent) -> None:
    data = json.loads(base64.b64decode(cloud_event.data["message"]["data"]))
    # Flush before processing to export a cumulative baseline (see inbox main.py:
    # Prometheus increase() needs ≥2 samples to show a non-zero result).
    otel.flush()
    try:
        match data.get("event"):
            case "email_classified":
                task_create.handle(data)
            case "label_applied":
                label_applied.handle(data)
            case other:
                logger.warning("Unknown event type %r — ignoring", other)
    except Exception:
        otel.errors.add(1, {"handler": str(data.get("event", "unknown"))})
        raise
    finally:
        otel.flush()


@functions_framework.http
def webhook(request):
    otel.flush()
    try:
        # Asana handshake — any request carrying X-Hook-Secret
        hook_secret = request.headers.get("X-Hook-Secret")
        if hook_secret:
            return asana_webhook.handshake(hook_secret)

        if request.path == "/escalate" and request.method == "POST":
            if not escalation.is_authorized(request.headers.get("Authorization")):
                return "", 401
            return escalation.run(), 200

        if request.method != "POST":
            return "", 405

        return asana_webhook.receive(
            request.get_data(), request.headers.get("X-Hook-Signature", "")
        )
    except Exception:
        otel.errors.add(1, {"handler": "webhook"})
        raise
    finally:
        otel.flush()

# ---------------------------------------------------------------------------
# Overdue escalation — daily at 6 AM ET
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "escalation" {
  name      = "tasks-escalation"
  schedule  = "0 6 * * *"
  time_zone = "America/New_York"

  http_target {
    http_method = "POST"
    uri         = "${google_cloudfunctions2_function.tasks_webhook.service_config[0].uri}/escalate"
    body        = base64encode("{}")
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${var.tasks_escalate_token}"
    }
  }
}

# ---------------------------------------------------------------------------
# Due-day digest — every 10 minutes; the route itself decides whether to
# rebuild (dirty flag from the Asana webhook, or last rebuild > 60 min old).
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "digest" {
  name      = "tasks-digest"
  schedule  = "*/10 * * * *"
  time_zone = "America/Los_Angeles"

  # The route is idempotent and the next tick is 10 minutes away, so an
  # overlapping retry buys nothing — let a slow or failed tick simply drop.
  attempt_deadline = "300s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloudfunctions2_function.tasks_webhook.service_config[0].uri}/digest"
    body        = base64encode("{}")
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${var.tasks_escalate_token}"
    }
  }
}

# ---------------------------------------------------------------------------
# Webhook reconciliation — daily. Asana deletes a webhook after 24 hours of
# failed delivery, so this is what makes a dropped registration self-healing
# rather than a silent end to recurrence in that project.
# ---------------------------------------------------------------------------
resource "google_cloud_scheduler_job" "webhook_sync" {
  name      = "tasks-webhook-sync"
  schedule  = "30 5 * * *"
  time_zone = "America/New_York"

  # Registration blocks on Asana's synchronous handshake round-trip per
  # project; five projects fit comfortably, a retry buys nothing before the
  # next daily tick.
  attempt_deadline = "300s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloudfunctions2_function.tasks_webhook.service_config[0].uri}/webhook-sync"
    body = base64encode(jsonencode({
      target = google_cloudfunctions2_function.tasks_webhook.service_config[0].uri
    }))
    headers = {
      "Content-Type"  = "application/json"
      "Authorization" = "Bearer ${var.tasks_escalate_token}"
    }
  }
}

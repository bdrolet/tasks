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

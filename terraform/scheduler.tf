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
    headers     = { "Content-Type" = "application/json" }
  }
}

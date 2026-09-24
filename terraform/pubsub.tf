# The email-events topic is owned by the INBOX repo's terraform (producer owns
# the event stream) — referenced read-only here. The tasks-events CF creates
# its own push subscription on it via its event_trigger block.
data "google_pubsub_topic" "email_events" {
  name    = "email-events"
  project = var.project_id
}

# task-events — owned here (this service is the producer). Carries
# task_changed (webhook, pipeline, API, heal) and day_changed (scheduler).
# Design: docs/superpowers/specs/2026-09-23-next-prioritizer-design.md (D2)
resource "google_pubsub_topic" "task_events" {
  name = "task-events"
}

resource "google_pubsub_topic_iam_member" "task_events_publishers" {
  for_each = {
    events  = google_service_account.tasks_events_cf.email
    webhook = google_service_account.tasks_webhook_cf.email
    api     = google_service_account.tasks_api.email
  }
  topic  = google_pubsub_topic.task_events.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${each.value}"
}

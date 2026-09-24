# ---------------------------------------------------------------------------
# tasks-events Cloud Function service account
# ---------------------------------------------------------------------------
resource "google_service_account" "tasks_events_cf" {
  account_id   = "tasks-events-cf"
  display_name = "Tasks Events Cloud Function"
}

resource "google_secret_manager_secret_iam_member" "events_cf_shared" {
  for_each = {
    for k, v in data.google_secret_manager_secret.shared : k => v
  }
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "events_cf_db_password" {
  secret_id = google_secret_manager_secret.tasks_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

# Both CFs call Claude: the events CF for enrichment (summary, deadline), the
# webhook CF for the due-day digest's Haiku-condensed bullets. The digest moved
# onto the webhook CF after this grant was first written, and without the key
# every rebuild fell back to unsummarized bullets — silently, because
# services/task_bullets.py treats a Claude failure as a fallback, not an error.
resource "google_secret_manager_secret_iam_member" "events_cf_anthropic" {
  secret_id = google_secret_manager_secret.tasks_anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_cf_anthropic" {
  secret_id = google_secret_manager_secret.tasks_anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

resource "google_project_iam_member" "events_cf_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

# ---------------------------------------------------------------------------
# tasks-webhook Cloud Function service account
# ---------------------------------------------------------------------------
resource "google_service_account" "tasks_webhook_cf" {
  account_id   = "tasks-webhook-cf"
  display_name = "Tasks Webhook Cloud Function"
}

# standing-context is deliberately excluded: the webhook CF never reads the
# declared facts, and it is the one function that must stay publicly invokable
# (Asana posts to it unauthenticated), so it gets no read access to personal
# data it has no use for.
resource "google_secret_manager_secret_iam_member" "webhook_cf_shared" {
  for_each = {
    for k, v in data.google_secret_manager_secret.shared : k => v
    if k != "standing-context"
  }
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_cf_db_password" {
  secret_id = google_secret_manager_secret.tasks_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

resource "google_project_iam_member" "webhook_cf_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "webhook_cf_webhook_secret" {
  count     = var.asana_webhook_secret == "" ? 0 : 1
  secret_id = google_secret_manager_secret.asana_webhook_secret[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

# /escalate is only reachable through the webhook CF — events CF never needs this.
resource "google_secret_manager_secret_iam_member" "webhook_cf_escalate_token" {
  secret_id = google_secret_manager_secret.tasks_escalate_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

# The email-events topic and inbox-process-cf's publisher binding live in the
# INBOX repo's terraform (producer owns the stream) — see plan Task 16 Step 2.

# ---------------------------------------------------------------------------
# tasks-prioritize Cloud Function service account — task-events subscriber.
# Reads Asana, calls Claude, writes the prioritizer tables; no calendar, no
# standing context, no webhook secrets.
# ---------------------------------------------------------------------------
resource "google_service_account" "tasks_prioritize_cf" {
  account_id   = "tasks-prioritize-cf"
  display_name = "Tasks Prioritize Cloud Function"
}

resource "google_secret_manager_secret_iam_member" "prioritize_cf_shared" {
  for_each = {
    for k, v in data.google_secret_manager_secret.shared : k => v
    if contains(["asana-api-key", "grafana-otlp-endpoint", "grafana-otlp-token"], k)
  }
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_prioritize_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "prioritize_cf_db_password" {
  secret_id = google_secret_manager_secret.tasks_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_prioritize_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "prioritize_cf_anthropic" {
  secret_id = google_secret_manager_secret.tasks_anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_prioritize_cf.email}"
}

resource "google_project_iam_member" "prioritize_cf_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.tasks_prioritize_cf.email}"
}

# The heal step republishes to its own topic.
resource "google_pubsub_topic_iam_member" "task_events_prioritize_publisher" {
  topic  = google_pubsub_topic.task_events.name
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${google_service_account.tasks_prioritize_cf.email}"
}

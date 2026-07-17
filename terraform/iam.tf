# ---------------------------------------------------------------------------
# tasks-events Cloud Function service account
# ---------------------------------------------------------------------------
resource "google_service_account" "tasks_events_cf" {
  account_id   = "tasks-events-cf"
  display_name = "Tasks Events Cloud Function"
}

resource "google_secret_manager_secret_iam_member" "events_cf_shared" {
  for_each  = data.google_secret_manager_secret.shared
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

resource "google_secret_manager_secret_iam_member" "events_cf_db_password" {
  secret_id = google_secret_manager_secret.tasks_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

# Enrichment runs only in the events CF — the webhook CF gets no Anthropic access.
resource "google_secret_manager_secret_iam_member" "events_cf_anthropic" {
  secret_id = google_secret_manager_secret.tasks_anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_events_cf.email}"
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

resource "google_secret_manager_secret_iam_member" "webhook_cf_shared" {
  for_each  = data.google_secret_manager_secret.shared
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

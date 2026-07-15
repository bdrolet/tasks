# Shared secrets created and version-managed by the inbox repo's terraform —
# referenced read-only here. Do NOT convert to resources: two states owning
# the same secret_id fails with "already exists".
data "google_secret_manager_secret" "shared" {
  for_each = toset([
    "asana-api-key",
    "grafana-otlp-endpoint",
    "grafana-otlp-token",
    "webhook-label-token",
    "search-token", # inbox-api bearer auth (clients/inbox_api.py)
  ])
  secret_id = each.key
  project   = var.project_id
}

# Owned by this repo, created in the second-pass apply once the value exists
# (captured from CF logs after webhook registration).
resource "google_secret_manager_secret" "asana_webhook_secret" {
  count     = var.asana_webhook_secret == "" ? 0 : 1
  secret_id = "asana-webhook-secret"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "asana_webhook_secret" {
  count       = var.asana_webhook_secret == "" ? 0 : 1
  secret      = google_secret_manager_secret.asana_webhook_secret[0].id
  secret_data = var.asana_webhook_secret
}

# Owned by this repo — password for the tasks Cloud SQL user.
resource "google_secret_manager_secret" "tasks_db_password" {
  secret_id = "tasks-db-password"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "tasks_db_password" {
  secret      = google_secret_manager_secret.tasks_db_password.id
  secret_data = var.tasks_db_password
}

# Owned by this repo — dedicated Anthropic key for task enrichment (separate
# spend tracking and rotation from inbox's anthropic-api-key). Created in the
# Anthropic Console, key name "tasks-cf" — see Task 17 / terraform.tfvars.example.
resource "google_secret_manager_secret" "tasks_anthropic_api_key" {
  secret_id = "tasks-anthropic-api-key"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "tasks_anthropic_api_key" {
  secret      = google_secret_manager_secret.tasks_anthropic_api_key.id
  secret_data = var.tasks_anthropic_api_key
}

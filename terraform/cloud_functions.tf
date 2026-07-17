locals {
  cf_source_bucket = "${var.project_id}-tasks-cf-source"

  # Env vars shared by both CFs
  common_env = {
    GCP_PROJECT_ID            = var.project_id
    ASANA_PROJECT_ID          = var.asana_project_id
    ASANA_SECTION_REVIEW_GID  = var.asana_section_review_gid
    ASANA_SECTION_RESPOND_GID = var.asana_section_respond_gid
    ASANA_SECTION_URGENT_GID  = var.asana_section_urgent_gid
    ASANA_SECTION_DONE_GID    = var.asana_section_done_gid
    ASANA_SECTION_OVERDUE_GID = var.asana_section_overdue_gid
    WEBHOOK_URL               = var.inbox_webhook_url
    INBOX_API_URL             = var.inbox_api_url
    CLOUD_SQL_CONNECTION_NAME = data.google_sql_database_instance.inbox.connection_name
    POSTGRES_USER             = google_sql_user.tasks.name
    POSTGRES_DB               = google_sql_database.tasks.name
  }
}

resource "google_storage_bucket" "cf_source" {
  name                        = local.cf_source_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = true
}

data "archive_file" "source" {
  type        = "zip"
  source_dir  = "${path.module}/.."
  output_path = "${path.module}/.terraform/tasks.zip"
  excludes = [
    "terraform",
    ".venv",
    ".git",
    ".github",
    ".claude",
    "docs",
    "tests",
    "scripts",
    ".env",
    ".pre-commit-config.yaml",
    "requirements-dev.txt",
    "conftest.py",
    # Bytecode/test caches: not gitignored from the archive_file's view (it
    # scans the filesystem, not git), so a local apply after running pytest
    # picks up whatever cache happens to exist and produces a non-reproducible
    # zip hash — every source package needs its own entry, no ** glob support.
    "__pycache__",
    "clients/__pycache__",
    "services/__pycache__",
    "handlers/__pycache__",
    "models/__pycache__",
    "repo/__pycache__",
    ".pytest_cache",
  ]
}

resource "google_storage_bucket_object" "source" {
  name   = "tasks-${data.archive_file.source.output_md5}.zip"
  bucket = google_storage_bucket.cf_source.name
  source = data.archive_file.source.output_path
}

# ---------------------------------------------------------------------------
# tasks-events — Pub/Sub-triggered event processor
# ---------------------------------------------------------------------------
resource "google_cloudfunctions2_function" "tasks_events" {
  name     = "tasks-events"
  location = var.region

  build_config {
    runtime     = "python313"
    entry_point = "process"
    source {
      storage_source {
        bucket = google_storage_bucket.cf_source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    service_account_email = google_service_account.tasks_events_cf.email
    min_instance_count    = 0
    max_instance_count    = 3
    timeout_seconds       = 120
    available_memory      = "512Mi"
    environment_variables = local.common_env

    secret_environment_variables {
      key        = "ASANA_API_KEY"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["asana-api-key"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "WEBHOOK_LABEL_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["webhook-label-token"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_ENDPOINT"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-endpoint"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-token"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "POSTGRES_PASSWORD"
      project_id = var.project_id
      secret     = google_secret_manager_secret.tasks_db_password.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "ANTHROPIC_API_KEY"
      project_id = var.project_id
      secret     = google_secret_manager_secret.tasks_anthropic_api_key.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "INBOX_API_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["search-token"].secret_id
      version    = "latest"
    }
  }

  event_trigger {
    trigger_region = var.region
    event_type     = "google.cloud.pubsub.topic.v1.messagePublished"
    pubsub_topic   = data.google_pubsub_topic.email_events.id
    retry_policy   = "RETRY_POLICY_RETRY"
  }
}

# ---------------------------------------------------------------------------
# tasks-webhook — HTTP-triggered (public): Asana webhooks + /escalate cron
# ---------------------------------------------------------------------------
resource "google_cloudfunctions2_function" "tasks_webhook" {
  name     = "tasks-webhook"
  location = var.region

  build_config {
    runtime     = "python313"
    entry_point = "webhook"
    source {
      storage_source {
        bucket = google_storage_bucket.cf_source.name
        object = google_storage_bucket_object.source.name
      }
    }
  }

  service_config {
    service_account_email = google_service_account.tasks_webhook_cf.email
    min_instance_count    = 0
    max_instance_count    = 3
    timeout_seconds       = 120
    available_memory      = "512Mi"
    environment_variables = local.common_env

    secret_environment_variables {
      key        = "ASANA_API_KEY"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["asana-api-key"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "WEBHOOK_LABEL_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["webhook-label-token"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_ENDPOINT"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-endpoint"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "GRAFANA_OTLP_TOKEN"
      project_id = var.project_id
      secret     = data.google_secret_manager_secret.shared["grafana-otlp-token"].secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "POSTGRES_PASSWORD"
      project_id = var.project_id
      secret     = google_secret_manager_secret.tasks_db_password.secret_id
      version    = "latest"
    }
    secret_environment_variables {
      key        = "ASANA_ESCALATE_TOKEN"
      project_id = var.project_id
      secret     = google_secret_manager_secret.tasks_escalate_token.secret_id
      version    = "latest"
    }

    # Injected only after the second-pass apply (webhook registration done,
    # var.asana_webhook_secret set in tfvars / GH secret).
    dynamic "secret_environment_variables" {
      for_each = var.asana_webhook_secret == "" ? [] : [1]
      content {
        key        = "ASANA_WEBHOOK_SECRET"
        project_id = var.project_id
        secret     = google_secret_manager_secret.asana_webhook_secret[0].secret_id
        version    = "latest"
      }
    }
  }
}

# Asana posts webhook events without auth — must be publicly invokable
resource "google_cloudfunctions2_function_iam_member" "webhook_public" {
  project        = var.project_id
  location       = var.region
  cloud_function = google_cloudfunctions2_function.tasks_webhook.name
  role           = "roles/cloudfunctions.invoker"
  member         = "allUsers"
}

# Gen2 CFs run on Cloud Run — also need the Cloud Run invoker for unauthenticated access
resource "google_cloud_run_v2_service_iam_member" "webhook_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloudfunctions2_function.tasks_webhook.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

output "webhook_url" {
  description = "tasks-webhook CF URL — use when registering the Asana webhook"
  value       = google_cloudfunctions2_function.tasks_webhook.service_config[0].uri
}

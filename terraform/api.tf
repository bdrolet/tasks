# ---------------------------------------------------------------------------
# tasks-api — Cloud Run FastAPI service (search / fetch / add / update tasks)
# Mirrors inbox-api (inbox repo terraform/api.tf). Public with app-level
# bearer auth; the token lives in the tasks-api-token secret owned here.
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "tasks" {
  repository_id = "tasks"
  format        = "DOCKER"
  location      = var.region
}

locals {
  api_image = "${var.region}-docker.pkg.dev/${var.project_id}/tasks/tasks-api:latest"
}

resource "google_secret_manager_secret" "tasks_api_token" {
  secret_id = "tasks-api-token"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "tasks_api_token" {
  secret      = google_secret_manager_secret.tasks_api_token.id
  secret_data = var.tasks_api_token
}

resource "google_service_account" "tasks_api" {
  account_id   = "tasks-api"
  display_name = "Tasks API Cloud Run"
}

resource "google_secret_manager_secret_iam_member" "api_shared" {
  for_each  = data.google_secret_manager_secret.shared
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_db_password" {
  secret_id = google_secret_manager_secret.tasks_db_password.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_api.email}"
}

resource "google_secret_manager_secret_iam_member" "api_token" {
  secret_id = google_secret_manager_secret.tasks_api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.tasks_api.email}"
}

resource "google_project_iam_member" "api_cloudsql" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.tasks_api.email}"
}

resource "google_artifact_registry_repository_iam_member" "api_ar_reader" {
  repository = google_artifact_registry_repository.tasks.name
  location   = var.region
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.tasks_api.email}"
}

resource "google_cloud_run_v2_service" "api" {
  name     = "tasks-api"
  location = var.region

  template {
    service_account = google_service_account.tasks_api.email
    timeout         = "60s"

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      # Placeholder until the first image push (see first-deploy runbook,
      # Task 14): terraform -target the AR repo, gcloud builds submit, then
      # full apply.
      image = local.api_image

      resources {
        limits = {
          memory = "512Mi"
        }
      }

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "ASANA_PROJECT_ID"
        value = var.asana_project_id
      }
      env {
        name  = "CLOUD_SQL_CONNECTION_NAME"
        value = data.google_sql_database_instance.inbox.connection_name
      }
      env {
        name  = "POSTGRES_USER"
        value = google_sql_user.tasks.name
      }
      env {
        name  = "POSTGRES_DB"
        value = google_sql_database.tasks.name
      }
      env {
        name = "ASANA_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["asana-api-key"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "TASKS_API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.tasks_api_token.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "POSTGRES_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.tasks_db_password.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GRAFANA_OTLP_ENDPOINT"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["grafana-otlp-endpoint"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GRAFANA_OTLP_TOKEN"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.shared["grafana-otlp-token"].secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # Image updated outside Terraform via gcloud run deploy (deploy-api.yml)
  lifecycle {
    ignore_changes = [template[0].containers[0].image]
  }

  depends_on = [google_artifact_registry_repository.tasks]
}

# Public — bearer-token auth enforced in app code (api/auth.py)
resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_artifact_registry_repository_iam_member" "deployer_ar_writer" {
  repository = google_artifact_registry_repository.tasks.name
  location   = var.region
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${var.deployer_sa}"
}

resource "google_cloud_run_v2_service_iam_member" "deployer_run_developer" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.developer"
  member   = "serviceAccount:${var.deployer_sa}"
}

output "tasks_api_url" {
  description = "tasks-api Cloud Run service URL"
  value       = google_cloud_run_v2_service.api.uri
}

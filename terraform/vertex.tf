# Vertex AI embeddings for semantic task search (clients/vertex.py).
# Spec deviation note: main.tf says project APIs are inbox-owned, but
# aiplatform is used only by tasks — owned here, never disabled on destroy.
resource "google_project_service" "aiplatform" {
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}

resource "google_project_iam_member" "events_cf_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.tasks_events_cf.email}"
}

resource "google_project_iam_member" "webhook_cf_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.tasks_webhook_cf.email}"
}

resource "google_project_iam_member" "tasks_api_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.tasks_api.email}"
}

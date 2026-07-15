variable "project_id" {
  description = "GCP project ID"
  type        = string
}

variable "region" {
  description = "GCP region for all resources"
  type        = string
  default     = "us-central1"
}

variable "asana_project_id" {
  description = "Asana project GID for inbox tasks (from https://app.asana.com/0/{gid}/list)"
  type        = string
}

variable "asana_section_review_gid" {
  description = "Asana section GID for Review tasks (open the section in Asana — numeric ID at URL end)"
  type        = string
  default     = ""
}

variable "asana_section_respond_gid" {
  description = "Asana section GID for Respond tasks"
  type        = string
  default     = ""
}

variable "asana_section_urgent_gid" {
  description = "Asana section GID for Urgent tasks (optional — unset leaves urgent tasks unsectioned)"
  type        = string
  default     = ""
}

variable "asana_section_done_gid" {
  description = "Asana section GID for Done tasks"
  type        = string
  default     = ""
}

variable "asana_section_overdue_gid" {
  description = "Asana section GID for Overdue (escalated) tasks"
  type        = string
  default     = ""
}

variable "asana_webhook_secret" {
  description = "X-Hook-Secret captured from the tasks-webhook CF logs after webhook registration. Empty until the second-pass apply (docs/asana-webhook-setup.md)."
  type        = string
  sensitive   = true
  default     = ""
}

variable "tasks_db_password" {
  description = "Password for the tasks Cloud SQL user. Generate with: openssl rand -base64 24 | tr -d '/+=' | head -c 32"
  type        = string
  sensitive   = true
}

variable "tasks_anthropic_api_key" {
  description = "Dedicated Anthropic API key for task enrichment — create at console.anthropic.com (key name: tasks-cf). Do NOT reuse inbox's key."
  type        = string
  sensitive   = true
}

variable "inbox_webhook_url" {
  description = "Inbox webhook CF URL — target of the task action links (Confirmed review / Respond instead / ...)"
  type        = string
  default     = "https://inbox-webhook-aizbgjlava-uc.a.run.app"
}

variable "inbox_api_url" {
  description = "inbox-api Cloud Run URL (mailbox gateway) — gcloud run services describe inbox-api --region us-central1 --format='value(status.url)'"
  type        = string
  default     = ""
}

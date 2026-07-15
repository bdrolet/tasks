#!/bin/bash
# Populate .env from Secret Manager + terraform.tfvars. Run from the repo root.
set -e
PROJECT=bens-project-462804
secret() { gcloud secrets versions access latest --secret="$1" --project="$PROJECT"; }
tfvar() { grep "^$1" terraform/terraform.tfvars | sed 's/.*= *"\(.*\)"/\1/'; }

cat > .env <<EOF
ASANA_API_KEY=$(secret asana-api-key)
ANTHROPIC_API_KEY=$(secret tasks-anthropic-api-key)
ASANA_PROJECT_ID=$(tfvar asana_project_id)
ASANA_SECTION_REVIEW_GID=$(tfvar asana_section_review_gid)
ASANA_SECTION_RESPOND_GID=$(tfvar asana_section_respond_gid)
ASANA_SECTION_URGENT_GID=$(tfvar asana_section_urgent_gid)
ASANA_SECTION_DONE_GID=$(tfvar asana_section_done_gid)
ASANA_SECTION_OVERDUE_GID=$(tfvar asana_section_overdue_gid)
ASANA_WEBHOOK_SECRET=$(secret asana-webhook-secret 2>/dev/null || echo "")
WEBHOOK_URL=https://inbox-webhook-aizbgjlava-uc.a.run.app
WEBHOOK_LABEL_TOKEN=$(secret webhook-label-token)
GRAFANA_OTLP_ENDPOINT=$(secret grafana-otlp-endpoint)
GRAFANA_OTLP_TOKEN=$(secret grafana-otlp-token)
INBOX_API_URL=$(tfvar inbox_api_url)
INBOX_API_TOKEN=$(secret search-token)
CLOUD_SQL_CONNECTION_NAME=bens-project-462804:us-central1:inbox
POSTGRES_USER=tasks
POSTGRES_DB=tasks
POSTGRES_PASSWORD=$(secret tasks-db-password 2>/dev/null || echo "")
EOF
echo ".env written"

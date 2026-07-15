---
name: adding-tasks-secret
description: Use when adding a new secret, API key, or credential to the tasks project — wiring it into .env, Terraform Secret Manager, the Cloud Function env vars, GitHub Actions secrets, and the CI deploy workflow.
metadata:
  depends-on: terraform-plan, terraform-apply
---

# Adding a Secret to the Tasks Stack

**First check whether the secret already exists in Secret Manager** (`gcloud secrets list --project=bens-project-462804`). Secrets owned by the inbox repo (created there — currently `asana-api-key`, `grafana-otlp-*`, `webhook-label-token`, `search-token`) must be referenced as a `data` source here — add the name to the `for_each` set in `terraform/secrets.tf` `data "google_secret_manager_secret" "shared"` and skip steps 2, 3, and 6 below (no value needed anywhere in this repo).

For a NEW secret named `my-secret` (kebab-case) / `MY_SECRET` (env var), 6 places change:

1. **`.env`** — `MY_SECRET=<value>`, and add the line to `scripts/fetch-env.sh`
2. **`terraform/variables.tf`** — `variable "my_secret" { type = string, sensitive = true, default = "" }`
3. **`terraform/secrets.tf`** — `google_secret_manager_secret` + `_version` resources (follow the `asana_webhook_secret` pattern; drop the `count` guard unless the value arrives late)
4. **`terraform/cloud_functions.tf`** — `secret_environment_variables` block on whichever CF needs it, plus an accessor binding in `terraform/iam.tf` for that CF's SA
5. **`terraform/terraform.tfvars.example`** (placeholder) and **`terraform/terraform.tfvars`** (real value, gitignored)
6. **`.github/workflows/deploy.yml`** — `TF_VAR_my_secret: ${{ secrets.TF_VAR_MY_SECRET }}` in the apply step's `env:`, then `gh secret set TF_VAR_MY_SECRET --body "<value>" --repo bdrolet/tasks`

Then: **terraform-plan** (confirm the new resources), get approval, **terraform-apply**.

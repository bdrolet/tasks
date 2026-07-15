---
name: deploy-tasks
description: Use when the user wants to deploy, release, or push code changes to the tasks-events or tasks-webhook Cloud Functions after changing main.py or any file under clients/, services/, handlers/, or models/.
metadata:
  depends-on:
    - terraform-apply
---

# Deploying the Tasks Cloud Functions

Both CFs (`tasks-events`, `tasks-webhook`) deploy from the same repo-root zip. Terraform re-zips, uploads to GCS, and redeploys whichever functions' source changed.

**Watched paths** (changing these requires a deploy): `main.py`, `clients/**`, `services/**`, `handlers/**`, `models/**`, `requirements.txt`, `terraform/**`.

## Steps

1. **REQUIRED:** Use the **terraform-apply** skill (user-level, repo-aware — it resolves this repo's terraform/ from the cwd) to run the apply.
2. Verify the new revision is live:
   ```bash
   gcloud functions describe tasks-events --project=bens-project-462804 --region=us-central1 --format='value(state,updateTime)'
   gcloud functions describe tasks-webhook --project=bens-project-462804 --region=us-central1 --format='value(state,updateTime)'
   ```
   Both should show `ACTIVE` with a fresh updateTime.
3. Use **fetch-tasks-logs** to check for startup errors in both CFs.
4. If a PR is open, post a deploy summary comment (apply result + post-deploy log tail).

Note: pushes to `main` touching the watched paths auto-deploy via `.github/workflows/deploy.yml` — use this skill for local/manual deploys and verification.

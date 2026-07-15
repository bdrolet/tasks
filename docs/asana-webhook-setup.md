# Asana Webhook Setup & Re-registration Runbook

One-time setup after the first terraform apply, and again any time the
`tasks-webhook` CF URL changes or the webhook goes dead.

## Register

```bash
scripts/fetch-env.sh    # needs ASANA_API_KEY + ASANA_PROJECT_ID
WEBHOOK_URL=$(cd terraform && terraform output -raw webhook_url)
.venv/bin/python scripts/register_webhook.py "$WEBHOOK_URL"
```

The script prints the webhook GID. The `X-Hook-Secret` went to the CF, which
logged it:

```bash
gcloud functions logs read tasks-webhook --project=bens-project-462804 \
  --region=us-central1 --limit=20 | grep 'X-Hook-Secret'
```

## Store the secret (second-pass apply)

1. `terraform/terraform.tfvars`: set `asana_webhook_secret = "<value>"` (no
   trailing whitespace — signature validation is exact).
2. GitHub secret: `gh secret set TF_VAR_ASANA_WEBHOOK_SECRET --body "<value>" --repo bdrolet/tasks`
3. `/terraform-plan` then `/terraform-apply` — creates the
   `asana-webhook-secret` SM secret and injects `ASANA_WEBHOOK_SECRET` into
   the webhook CF.

## Verify

```bash
gcloud functions describe tasks-webhook --project=bens-project-462804 \
  --region=us-central1 --format='yaml(serviceConfig.secretEnvironmentVariables)'
```
Then change any task in Asana and check the CF logs for
`signature_valid: true`.

## List / delete existing webhooks

```bash
source .env
WORKSPACE=$(curl -s -H "Authorization: Bearer $ASANA_API_KEY" \
  "https://app.asana.com/api/1.0/projects/$ASANA_PROJECT_ID?opt_fields=workspace" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['data']['workspace']['gid'])")
curl -s -H "Authorization: Bearer $ASANA_API_KEY" \
  "https://app.asana.com/api/1.0/webhooks?workspace=$WORKSPACE" | python3 -m json.tool
# delete: curl -X DELETE -H "Authorization: Bearer $ASANA_API_KEY" https://app.asana.com/api/1.0/webhooks/<gid>
```

## Where the secret lives (and healing when it's lost)

The `X-Hook-Secret` exists in exactly three places, in order of authority:

1. **Secret Manager `asana-webhook-secret`** — authoritative once stored;
   injected into the CF as `ASANA_WEBHOOK_SECRET`.
2. `terraform/terraform.tfvars` + GitHub secret `TF_VAR_ASANA_WEBHOOK_SECRET`
   — feed Secret Manager on apply; keep in sync with (1).
3. The `tasks-webhook` CF log line from the handshake — **expires after 30
   days** (Cloud Logging retention). This is only a bootstrap channel.

**If the secret is lost** (SM value missing/wrong and the handshake log has
expired), it cannot be recovered — Asana never re-sends it. Heal by minting a
fresh one:

1. List webhooks (below) and DELETE the existing registration.
2. Re-run the **Register** section — the new handshake logs a new secret.
3. Re-run **Store the secret** with the new value (tfvars + GH secret + apply).

Events sent between webhook death and re-registration are dropped; completions
made in that window won't move to Done and need a manual sweep in Asana.

## Troubleshooting

- **Handshake fails (non-200)**: CF must be public — check the `allUsers`
  invoker bindings in `terraform/cloud_functions.tf`; check CF logs.
- **register_webhook.py times out**: Asana retries the handshake — check CF
  logs before re-running; the webhook may have registered anyway (list it).
- **`signature_valid` false on every event**: stored secret has trailing
  whitespace or is from an older registration — follow the healing steps above.
- **CF URL changed** (function recreated): the old webhook is dead — delete
  it, re-register, re-store the new secret.

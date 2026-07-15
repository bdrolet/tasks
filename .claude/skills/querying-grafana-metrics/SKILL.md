---
name: querying-grafana-metrics
description: Use when checking whether tasks OTel metrics have landed in Grafana Cloud, running a PromQL query against the inbox Prometheus datasource, verifying that a deployed change is emitting metrics, or inspecting metric values and labels for asana_* series.
---

## Credentials

Stored in `.env` (project root) and `~/src/scripts/zsh/config/.secrets`:

| Env var | Purpose |
|---|---|
| `GRAFANA_PROM_URL` | `https://prometheus-prod-67-prod-us-west-0.grafana.net/prometheus` |
| `GRAFANA_PROM_INSTANCE_ID` | `3286064` |
| `GRAFANA_PROM_TOKEN` | Raw `glc_...` read token |

## Querying

```python
import base64, os, urllib.request, urllib.parse, json
from dotenv import load_dotenv
load_dotenv()  # loads .env from project root

auth = "Basic " + base64.b64encode(
    f"{os.environ['GRAFANA_PROM_INSTANCE_ID']}:{os.environ['GRAFANA_PROM_TOKEN']}".encode()
).decode()
base = os.environ["GRAFANA_PROM_URL"] + "/api/v1"

def query(promql):
    params = urllib.parse.urlencode({"query": promql})
    req = urllib.request.Request(f"{base}/query?{params}", headers={"Authorization": auth})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["data"]["result"]
```

Or with curl:
```bash
source ~/src/scripts/zsh/config/.secrets
curl -su "$GRAFANA_PROM_INSTANCE_ID:$GRAFANA_PROM_TOKEN" \
  --data-urlencode "query=asana_tasks_created_total" \
  "$GRAFANA_PROM_URL/api/v1/query" | python3 -m json.tool
```

## Key metrics

| Metric | Labels |
|---|---|
| `asana_tasks_created_total` | `category`, `importance`, `service_name` |
| `asana_tasks_moved_total` | `from_section`, `to_section` |
| `asana_tasks_completed_total` | — |
| `asana_escalations_total` | — |
| `asana_errors_total` | `handler` |
| `asana_claude_tokens_total` | `token_type` (input\|output) |
| `asana_api_duration_milliseconds_bucket` | `operation` |

## Useful PromQL

```promql
# Tasks created by category
sum by (category) (asana_tasks_created_total)

# Section move flow
sum by (from_section, to_section) (asana_tasks_moved_total)

# p95 Asana API latency by operation
histogram_quantile(0.95, sum by (le, operation) (rate(asana_api_duration_milliseconds_bucket[1h])))

# Error rate by handler
sum by (handler) (rate(asana_errors_total[1h]))
```

## Notes

- Mimir ingestion lag is ~10–60s after a flush — if a metric just fired, wait before querying
- `service_name` values are `tasks-events` / `tasks-webhook` (from `K_SERVICE`), `tasks-local` for local runs
- The write token (`GRAFANA_OTLP_TOKEN`) is base64-encoded; the read token (`GRAFANA_PROM_TOKEN`) is the raw `glc_` string

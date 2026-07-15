# OTel Metrics

All instruments live in `clients/otel.py` (prefix `asana.`), exported to
Grafana Cloud OTLP every 60s + force-flushed around each CF invocation.
No-ops when `GRAFANA_OTLP_ENDPOINT` is unset. Style reference:
inbox `docs/otel-metrics-in-cloud-functions.md`.

| Instrument | Type | Attributes | Recorded in |
|---|---|---|---|
| `asana.tasks.created` | Counter | `category`, `importance` | handlers/task_create.py |
| `asana.tasks.moved` | Counter | `from_section`, `to_section` | label_applied, task_complete, escalation |
| `asana.tasks.completed` | Counter | — | handlers/task_complete.py |
| `asana.escalations` | Counter | — | services/escalation.py |
| `asana.errors` | Counter | `handler` | main.py (both entry points) |
| `asana.claude.tokens` | Counter | `token_type` | clients/claude.py (enrichment spend) |
| `asana.api.duration` | Histogram (ms) | `operation` | clients/asana.py `_request` |

## Prometheus names

OTLP → Mimir renders dots as underscores and appends units/suffixes:
`asana_tasks_created_total`, `asana_tasks_moved_total`,
`asana_tasks_completed_total`, `asana_escalations_total`,
`asana_errors_total`, `asana_api_duration_milliseconds_bucket`.

## Example PromQL

```promql
sum by (category, importance) (asana_tasks_created_total)
sum by (from_section, to_section) (increase(asana_tasks_moved_total[1d]))
histogram_quantile(0.95, sum by (le, operation) (rate(asana_api_duration_milliseconds_bucket[1h])))
sum by (handler) (rate(asana_errors_total[1h])) > 0
```

Query credentials + how-to: `/querying-grafana-metrics` skill. `service_name`
is `tasks-events` or `tasks-webhook` (from `K_SERVICE`); local runs report
`tasks-local`.

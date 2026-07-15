---
name: adding-observability
description: Use when adding a new pipeline stage, service, handler, or Pub/Sub flow to the inbox project, or when modifying existing code that should emit new metrics or traces. Also use when asked how to add a span, record a metric, or propagate trace context through a new Pub/Sub publish.
---

## OTel setup

All metric instruments and provider setup live in `clients/otel.py`. It exports:
- `setup_telemetry(service_name)` — called once at module level in `main.py`
- `flush()` — called in `finally` at end of every Cloud Function invocation
- `get_tracer()` — returns the active tracer
- Pre-built counters and histograms (see below)

Both CFs deploy from the repo root and share `clients/otel.py`; service name comes from `K_SERVICE`.

No-ops when `GRAFANA_OTLP_ENDPOINT` is unset (local dev without credentials).

## Adding a span to a new pipeline stage

Add a child span inside the existing `inbox.process` root span in `handlers/pipeline.py`:

```python
with otel.get_tracer().start_as_current_span("tasks.<stage>") as span:
    span.set_attribute("key", value)
    result = do_work()
    # on error:
    span.set_status(StatusCode.ERROR)
    span.record_exception(e)
```

Span names use `tasks.<verb>` format (e.g. `tasks.fetch`, `tasks.classify`).

## Adding a new metric instrument

Define it in `clients/otel.py` alongside the existing instruments (in `setup_telemetry` and as a module-level no-op initializer):

```python
my_counter = meter.create_counter("asana.<name>", description="...")
my_hist    = meter.create_histogram("asana.<name>", unit="ms")
```

Then record it at the call site:

```python
import clients.otel as otel
otel.my_counter.add(1, {"attr": value})
```

Instruments are no-ops before `setup_telemetry()` runs — safe to call in tests and local dev.

## Recording API call duration

Use `api_duration` with an `operation` attribute. Wrap the call with `time.monotonic()` alongside the span — recorded centrally in `clients/asana.py::_request`:

```python
t0 = time.monotonic()
with otel.get_tracer().start_as_current_span("tasks.<operation>") as span:
    result = do_work()
otel.api_duration.record((time.monotonic() - t0) * 1000, {"operation": "<operation>"})
```

This makes API latency queryable as `asana_api_duration_milliseconds{operation="<operation>"}` in Grafana independently of the trace.

## Propagating trace context through a new Pub/Sub publish

**Publisher side** — inject before `publisher.publish()`:
```python
from opentelemetry.propagate import inject
carrier = {}
inject(carrier)
publisher.publish(topic, data.encode(), **carrier)
```

**Consumer side** — extract before starting the root span:
```python
from opentelemetry.propagate import extract
ctx = extract(cloud_event.data["message"].get("attributes", {}))
with tracer.start_as_current_span("inbox.<handler>", context=ctx):
    ...
```

## Cloud Function flush

Any new Cloud Function entry point must flush telemetry before returning:

```python
try:
    do_work()
finally:
    otel.flush()  # blocks up to 30s; exports pending spans + metrics
```

Add `setup_telemetry("inbox-<service>")` at module level (outside the handler) so providers survive across warm re-use. Add `GRAFANA_OTLP_ENDPOINT` and `GRAFANA_OTLP_TOKEN` secret env vars to the new CF in `terraform/cloud_functions.tf` following the existing pattern.

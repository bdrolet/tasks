import logging
import os

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_meter_provider: MeterProvider | None = None
_tracer_provider: TracerProvider | None = None
_metric_reader: PeriodicExportingMetricReader | None = None

# Metric instruments — no-ops until setup_telemetry() runs
tasks_created: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
tasks_moved: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
tasks_completed: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
escalations: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
errors: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
claude_tokens: metrics.Counter = metrics.NoOpMeter("noop").create_counter("noop")
api_duration: metrics.Histogram = metrics.NoOpMeter("noop").create_histogram("noop")


def setup_telemetry(service_name: str) -> None:
    """
    Initialize OTel MeterProvider, TracerProvider, and LoggerProvider targeting
    Grafana Cloud OTLP. No-ops when GRAFANA_OTLP_ENDPOINT is unset (local dev).
    """
    global _meter_provider, _tracer_provider, _metric_reader
    global tasks_created, tasks_moved, tasks_completed, escalations, errors
    global claude_tokens, api_duration

    endpoint = os.environ.get("GRAFANA_OTLP_ENDPOINT")
    if not endpoint:
        return

    token = os.environ.get("GRAFANA_OTLP_TOKEN", "")
    headers = {"Authorization": f"Basic {token}"}
    resource = Resource({"service.name": service_name})

    # --- Traces ---
    _tracer_provider = TracerProvider(resource=resource)
    _tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces", headers=headers))
    )
    trace.set_tracer_provider(_tracer_provider)

    # --- Metrics ---
    _metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers),
        export_interval_millis=60_000,
    )
    _meter_provider = MeterProvider(resource=resource, metric_readers=[_metric_reader])
    metrics.set_meter_provider(_meter_provider)

    meter = _meter_provider.get_meter(service_name)
    tasks_created = meter.create_counter(
        "asana.tasks.created", description="Asana tasks created from inbox events"
    )
    tasks_moved = meter.create_counter(
        "asana.tasks.moved", description="Asana tasks moved between sections"
    )
    tasks_completed = meter.create_counter(
        "asana.tasks.completed", description="Asana tasks completed (via webhook)"
    )
    escalations = meter.create_counter("asana.escalations", description="Overdue tasks escalated")
    errors = meter.create_counter("asana.errors", description="Handler errors by handler name")
    claude_tokens = meter.create_counter(
        "asana.claude.tokens", description="Claude tokens spent on task enrichment"
    )
    api_duration = meter.create_histogram(
        "asana.api.duration", unit="ms", description="Asana REST call duration by operation"
    )

    # --- Logs ---
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", headers=headers))
    )
    set_logger_provider(log_provider)
    logging.getLogger().addHandler(LoggingHandler(logger_provider=log_provider))

    logger.debug("OTel telemetry configured for service=%s endpoint=%s", service_name, endpoint)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer("tasks")


def flush() -> None:
    """Force-flush all providers. Call before and after every Cloud Function invocation."""
    if _tracer_provider is not None:
        _tracer_provider.force_flush(timeout_millis=5_000)
    if _metric_reader is not None:
        _metric_reader.force_flush(timeout_millis=5_000)

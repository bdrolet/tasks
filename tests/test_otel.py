import clients.otel as otel


def test_instruments_are_noop_safe_without_setup():
    otel.tasks_created.add(1, {"category": "review", "importance": "P1"})
    otel.tasks_moved.add(1, {"from_section": "Review", "to_section": "Done"})
    otel.tasks_completed.add(1)
    otel.escalations.add(1)
    otel.errors.add(1, {"handler": "task_create"})
    otel.claude_tokens.add(10, {"token_type": "input"})
    otel.api_duration.record(12.5, {"operation": "create_task"})


def test_setup_is_noop_without_endpoint(monkeypatch):
    monkeypatch.delenv("GRAFANA_OTLP_ENDPOINT", raising=False)
    otel.setup_telemetry("tasks-test")
    otel.flush()  # must not raise


def test_api_requests_counter_exists():
    import clients.otel as otel

    # NoOp until setup_telemetry(); must exist and accept add() without error.
    otel.api_requests.add(1, {"route": "/search", "status": "200"})

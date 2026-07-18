import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


def test_healthz_returns_ok():
    from api.main import app

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_verify_token_noop_when_env_unset(monkeypatch):
    from api.auth import verify_token

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("TASKS_API_TOKEN", raising=False)
    assert verify_token(None) is None  # no exception


def test_verify_token_rejects_missing_and_wrong(monkeypatch):
    from api.auth import verify_token

    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")
    with pytest.raises(HTTPException) as exc:
        verify_token(None)
    assert exc.value.status_code == 401


def test_verify_token_fails_closed_when_k_service_set_and_token_unset(monkeypatch):
    from api.auth import verify_token

    monkeypatch.setenv("K_SERVICE", "tasks-api")
    monkeypatch.delenv("TASKS_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as exc:
        verify_token(None)
    assert exc.value.status_code == 503


def test_verify_token_noop_when_k_service_unset_and_token_unset(monkeypatch):
    from api.auth import verify_token

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.delenv("TASKS_API_TOKEN", raising=False)
    assert verify_token(None) is None


def test_verify_token_allows_correct_token_when_k_service_set(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials

    from api.auth import verify_token

    monkeypatch.setenv("K_SERVICE", "tasks-api")
    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="sekrit")
    assert verify_token(creds) is None


def test_unmatched_route_metric_uses_bounded_label(monkeypatch):
    import api.main as main_mod

    calls = []

    class FakeCounter:
        def add(self, amount, attributes=None):
            calls.append(attributes)

    monkeypatch.setattr(main_mod.otel, "api_requests", FakeCounter())

    resp = TestClient(main_mod.app).get("/nope")
    assert resp.status_code == 404
    assert calls[-1]["route"] == "unmatched"


def _status_error(status: int, body: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://app.asana.com/api/1.0/tasks/1")
    response = httpx.Response(status, json=body or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


def test_translate_asana_errors_passes_through_4xx():
    from api.errors import translate_asana_errors

    with pytest.raises(HTTPException) as exc:
        with translate_asana_errors():
            raise _status_error(404, {"errors": [{"message": "task not found"}]})
    assert exc.value.status_code == 404
    assert "task not found" in exc.value.detail


def test_translate_asana_errors_maps_5xx_to_502():
    from api.errors import translate_asana_errors

    with pytest.raises(HTTPException) as exc:
        with translate_asana_errors():
            raise _status_error(500)
    assert exc.value.status_code == 502


def test_translate_asana_errors_maps_transport_errors_to_502():
    from api.errors import translate_asana_errors

    with pytest.raises(HTTPException) as exc:
        with translate_asana_errors():
            raise httpx.ConnectTimeout("timed out")
    assert exc.value.status_code == 502

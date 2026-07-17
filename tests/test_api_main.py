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

    monkeypatch.delenv("TASKS_API_TOKEN", raising=False)
    assert verify_token(None) is None  # no exception


def test_verify_token_rejects_missing_and_wrong(monkeypatch):
    from api.auth import verify_token

    monkeypatch.setenv("TASKS_API_TOKEN", "sekrit")
    with pytest.raises(HTTPException) as exc:
        verify_token(None)
    assert exc.value.status_code == 401


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

import base64
import json
import time

import pytest

from clients import gcp_auth


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"hdr.{payload}.sig"


@pytest.fixture(autouse=True)
def _fresh_cache():
    gcp_auth.reset_cache()
    yield
    gcp_auth.reset_cache()


def test_mints_once_per_audience_while_fresh(monkeypatch):
    calls = []

    def fake_mint(aud):
        calls.append(aud)
        return _jwt(time.time() + 3600)

    monkeypatch.setattr(gcp_auth, "_mint", fake_mint)
    t1 = gcp_auth.id_token_for("https://a.example")
    t2 = gcp_auth.id_token_for("https://a.example")
    gcp_auth.id_token_for("https://b.example")
    assert t1 == t2
    assert calls == ["https://a.example", "https://b.example"]


def test_refreshes_inside_margin(monkeypatch):
    calls = []

    def fake_mint(aud):
        calls.append(aud)
        return _jwt(time.time() + gcp_auth.REFRESH_MARGIN_S - 1)

    monkeypatch.setattr(gcp_auth, "_mint", fake_mint)
    gcp_auth.id_token_for("https://a.example")
    gcp_auth.id_token_for("https://a.example")
    assert len(calls) == 2


def test_off_cloud_run_uses_gcloud(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    seen = {}

    def fake_check_output(cmd, text):
        seen["cmd"] = cmd
        return "tok\n"

    monkeypatch.setattr(gcp_auth.subprocess, "check_output", fake_check_output)
    assert gcp_auth._mint("https://a.example") == "tok"
    assert seen["cmd"] == ["gcloud", "auth", "print-identity-token"]


def test_on_cloud_run_uses_metadata_server(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "svc")
    import google.oauth2.id_token

    monkeypatch.setattr(google.oauth2.id_token, "fetch_id_token", lambda req, aud: f"tok-for-{aud}")
    assert gcp_auth._mint("https://a.example") == "tok-for-https://a.example"

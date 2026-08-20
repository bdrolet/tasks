import httpx
import pytest

import clients.inbox_api as inbox_api


def _capture(monkeypatch, status=200, payload=None):
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(status, json=payload or {}, request=httpx.Request("GET", url))

    monkeypatch.setattr(inbox_api.httpx, "get", fake_get)
    return calls


@pytest.fixture(autouse=True)
def configure(monkeypatch):
    monkeypatch.setattr(inbox_api, "INBOX_API_URL", "https://inbox-api.example")
    monkeypatch.setattr(inbox_api, "INBOX_API_TOKEN", "tok")


def test_get_email_hits_endpoint_with_bearer(monkeypatch):
    calls = _capture(monkeypatch, payload={"subject": "hi"})
    assert inbox_api.get_email("m1") == {"subject": "hi"}
    assert calls[0]["url"] == "https://inbox-api.example/emails/m1"
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["timeout"] == inbox_api.SEARCH_TIMEOUT


def test_get_attachments_endpoint(monkeypatch):
    calls = _capture(monkeypatch, payload={"attachments": []})
    inbox_api.get_attachments("m1")
    assert calls[0]["url"] == "https://inbox-api.example/emails/m1/attachments"


def test_http_error_raises(monkeypatch):
    _capture(monkeypatch, status=404)
    with pytest.raises(httpx.HTTPStatusError):
        inbox_api.get_email("missing")


def _capture_post(monkeypatch, status=200, payload=None):
    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        return httpx.Response(status, json=payload or {}, request=httpx.Request("POST", url))

    monkeypatch.setattr(inbox_api.httpx, "post", fake_post)
    return calls


def test_search_posts_query_and_returns_results(monkeypatch):
    calls = _capture_post(monkeypatch, payload={"results": [{"subject": "Bill"}]})
    rows = inbox_api.search("from:xfinity", limit=5)
    assert rows == [{"subject": "Bill"}]
    assert calls[0]["url"] == "https://inbox-api.example/search"
    assert calls[0]["json"] == {"query": "from:xfinity", "mode": "graph", "limit": 5}
    assert calls[0]["headers"]["Authorization"] == "Bearer tok"
    assert calls[0]["timeout"] == inbox_api.SEARCH_TIMEOUT


def test_search_passes_mode_and_mailboxes(monkeypatch):
    calls = _capture_post(monkeypatch, payload={"results": []})
    inbox_api.search("disney", mode="db", mailboxes=["me"])
    assert calls[0]["json"]["mode"] == "db"
    assert calls[0]["json"]["mailboxes"] == ["me"]


def test_search_http_error_raises(monkeypatch):
    _capture_post(monkeypatch, status=500)
    with pytest.raises(httpx.HTTPStatusError):
        inbox_api.search("x")

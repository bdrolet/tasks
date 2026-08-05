import math

import httpx
import pytest

import clients.vertex as vertex


class FakeResponse:
    def __init__(self, values, status=200):
        self._values = values
        self.status_code = status

    def json(self):
        return {"predictions": [{"embeddings": {"values": self._values}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setattr(vertex, "_token", lambda: "tok")


def test_embed_posts_task_type_and_dims(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return FakeResponse([3.0, 4.0])

    monkeypatch.setattr(httpx, "post", fake_post)
    vec = vertex.embed("hello", task_type="RETRIEVAL_QUERY")
    assert "gemini-embedding-001:predict" in captured["url"]
    assert captured["json"]["instances"][0]["task_type"] == "RETRIEVAL_QUERY"
    assert captured["json"]["parameters"]["outputDimensionality"] == vertex.EMBED_DIMS
    assert captured["headers"]["Authorization"] == "Bearer tok"
    # renormalized: [3,4] has norm 5 → [0.6, 0.8]
    assert vec == pytest.approx([0.6, 0.8])
    assert math.isclose(sum(x * x for x in vec), 1.0)


def test_embed_truncates_input(monkeypatch):
    captured = {}

    def fake_post(url, *, json, headers, timeout):
        captured["content"] = json["instances"][0]["content"]
        return FakeResponse([1.0])

    monkeypatch.setattr(httpx, "post", fake_post)
    vertex.embed("x" * 20_000, task_type="RETRIEVAL_DOCUMENT")
    assert len(captured["content"]) == 8000


def test_embed_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda url, **kw: FakeResponse([1.0], status=500)
    )
    with pytest.raises(httpx.HTTPStatusError):
        vertex.embed("hello", task_type="RETRIEVAL_QUERY")


def test_normalize_zero_vector_unchanged():
    assert vertex._normalize([0.0, 0.0]) == [0.0, 0.0]

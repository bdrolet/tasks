"""Vertex AI embedding calls — I/O only. IAM-authenticated (ADC locally,
metadata server in GCP); no API key, no secret. Provider-generic enough to
lift into the docs repo later (its spec §15 defers exactly this)."""

import logging
import math
import os
import time

import httpx

import clients.otel as otel

logger = logging.getLogger(__name__)

VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "bens-project-462804")
VERTEX_REGION = os.environ.get("VERTEX_REGION", "us-central1")
VERTEX_EMBED_MODEL = os.environ.get("VERTEX_EMBED_MODEL", "gemini-embedding-001")

EMBED_DIMS = 768
_MAX_CHARS = 8000  # model input limit is 2048 tokens; tasks never get near this

_credentials = None


def _token() -> str:
    global _credentials
    # Deferred import — google.auth pulls in a dependency tree the CFs
    # shouldn't pay for at cold start unless embedding is actually used.
    import google.auth
    import google.auth.transport.requests

    if _credentials is None:
        _credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
    if not _credentials.valid:
        _credentials.refresh(google.auth.transport.requests.Request())
    return _credentials.token


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def embed(text: str, *, task_type: str) -> list[float]:
    """text → 768-dim unit vector. task_type: RETRIEVAL_DOCUMENT (indexing)
    or RETRIEVAL_QUERY (searching). Raises on any failure — callers own the
    best-effort policy."""
    url = (
        f"https://{VERTEX_REGION}-aiplatform.googleapis.com/v1"
        f"/projects/{VERTEX_PROJECT}/locations/{VERTEX_REGION}"
        f"/publishers/google/models/{VERTEX_EMBED_MODEL}:predict"
    )
    t0 = time.monotonic()
    try:
        resp = httpx.post(
            url,
            json={
                "instances": [{"content": text[:_MAX_CHARS], "task_type": task_type}],
                "parameters": {"outputDimensionality": EMBED_DIMS},
            },
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception:
        otel.errors.add(1, {"handler": "vertex_embed"})
        raise
    finally:
        otel.vertex_duration.record(
            (time.monotonic() - t0) * 1000, {"model": VERTEX_EMBED_MODEL}
        )
    values = resp.json()["predictions"][0]["embeddings"]["values"]
    # Matryoshka truncation to 768 dims leaves vectors non-unit-length —
    # renormalize so pgvector cosine distance behaves.
    return _normalize(values)

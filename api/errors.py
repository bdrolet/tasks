"""Translate Asana/httpx failures into HTTP responses. Asana 400/403/404
pass through with Asana's message (client mistakes stay client errors);
everything else — 5xx, 429, timeouts — surfaces as 502."""

from contextlib import contextmanager
from typing import Iterator

import httpx
from fastapi import HTTPException


@contextmanager
def translate_asana_errors() -> Iterator[None]:
    try:
        yield
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        try:
            detail = exc.response.json().get("errors", [{}])[0].get("message", "")
        except Exception:
            detail = exc.response.text[:200]
        if status in (400, 403, 404):
            raise HTTPException(status_code=status, detail=f"Asana: {detail}") from exc
        raise HTTPException(status_code=502, detail=f"Asana error {status}: {detail}") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Asana unreachable: {exc}") from exc

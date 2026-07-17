"""Bearer-token auth for tasks-api. Single static token (tasks-api-token
secret); per-client tokens are a later concern. No-op when TASKS_API_TOKEN
is unset so local dev and tests run without auth."""

import os

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


def verify_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    expected = os.environ.get("TASKS_API_TOKEN")
    if not expected:
        return
    if credentials is None or credentials.credentials != expected:
        raise HTTPException(status_code=401)

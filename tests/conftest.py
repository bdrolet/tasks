"""Keep the test suite off the real database.

`handlers/asana_webhook.py::_mark_digest_dirty` opens a connection
best-effort, and `clients/db.py` reads its target from the environment. A
developer who has sourced `.env` (see scripts/fetch-env.sh) would otherwise
point the pre-existing webhook tests at production Cloud SQL. Unset the
connection vars for every test; tests that need a DB patch `get_conn`.
"""

import pytest

_DB_ENV = (
    "CLOUD_SQL_CONNECTION_NAME",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
)


@pytest.fixture(autouse=True)
def _no_real_db(monkeypatch):
    for name in _DB_ENV:
        monkeypatch.delenv(name, raising=False)

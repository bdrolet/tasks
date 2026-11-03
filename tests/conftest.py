"""Keep the test suite off the real database.

`handlers/asana_webhook.py::_mark_digest_dirty` opens a connection
best-effort, and `clients/db.py` reads its target from the environment. A
developer who has sourced `.env` (see scripts/fetch-env.sh) would otherwise
point the pre-existing webhook tests at production Cloud SQL. Unset the
connection vars for every test; tests that need a DB patch `get_conn`.
"""

import re

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


@pytest.fixture
def five_point_day(tmp_path, monkeypatch):
    """Prioritizer fixtures were computed for a 5-point day; pin that
    regardless of the tuned `capacity.points_per_day` in config/prioritize.toml."""
    from services import prioritize_config as pc

    text = re.sub(r"(?m)^points_per_day = .*$", "points_per_day = 5", pc.DEFAULT_PATH.read_text())
    p = tmp_path / "prioritize.toml"
    p.write_text(text)
    monkeypatch.setenv("PRIORITIZE_CONFIG_PATH", str(p))
    return p

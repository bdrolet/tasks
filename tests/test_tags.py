import sys
from unittest.mock import MagicMock

# psycopg needs libpq; mock it so this module — the only one that imports
# clients.db — can be collected in environments without libpq installed.
sys.modules["psycopg"] = MagicMock()
sys.modules["psycopg.rows"] = MagicMock()
sys.modules["psycopg.types"] = MagicMock()
sys.modules["psycopg.types.json"] = MagicMock()

import clients.asana as asana  # noqa: E402
from repo import asana_tags  # noqa: E402
from services import tags  # noqa: E402
from tests.test_repo import FakeConn  # noqa: E402


def test_resolve_gids_uses_db_cache(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_API_KEY", "test-key")
    monkeypatch.setattr(tags, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana_tags, "get_gid", lambda conn, name: "cached-gid")
    api_calls = []
    monkeypatch.setattr(asana, "find_tag", lambda name, wgid: api_calls.append(name))

    assert tags.resolve_gids(["finance"]) == ["cached-gid"]
    assert api_calls == []  # cache hit — no API lookup


def test_resolve_gids_falls_back_to_api_and_stores(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_API_KEY", "test-key")
    monkeypatch.setattr(tags, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(asana, "get_workspace_gid", lambda: "ws-1")
    monkeypatch.setattr(asana_tags, "get_gid", lambda conn, name: None)
    monkeypatch.setattr(asana, "find_tag", lambda name, wgid: None)
    monkeypatch.setattr(asana, "create_tag", lambda name, wgid: f"gid-{name}")
    stored = []
    monkeypatch.setattr(asana_tags, "store_gid", lambda conn, name, gid: stored.append((name, gid)))

    assert tags.resolve_gids(["finance"]) == ["gid-finance"]
    assert stored == [("finance", "gid-finance")]


def test_resolve_gids_empty_without_key(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_API_KEY", "")
    assert tags.resolve_gids(["finance"]) == []


def test_resolve_gids_survives_db_failure(monkeypatch):
    monkeypatch.setattr(asana, "ASANA_API_KEY", "test-key")

    def boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(tags, "get_conn", boom)

    assert tags.resolve_gids(["finance"]) == []

import clients.asana as asana
from repo import asana_tags
from services import tags
from tests.test_repo import FakeConn


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

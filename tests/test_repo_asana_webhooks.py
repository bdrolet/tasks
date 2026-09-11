from repo import asana_webhooks as repo
from tests.test_repo import FakeConn
from tests.test_repo_due_digest import RowsConn


def test_upsert_replaces_the_secret_and_clears_the_webhook_gid():
    conn = FakeConn()
    repo.upsert_secret(conn, "p1", "shh")
    query, params = conn.executed[0]
    assert "INSERT INTO asana_webhooks" in query
    assert "ON CONFLICT (project_gid) DO UPDATE" in query
    assert "webhook_gid = NULL" in query
    assert params == ("p1", "shh")


def test_set_webhook_gid_updates_by_project():
    conn = FakeConn()
    repo.set_webhook_gid(conn, "p1", "w1")
    query, params = conn.executed[0]
    assert "UPDATE asana_webhooks" in query
    assert params == ("w1", "p1")


def test_get_secret_returns_none_when_absent():
    assert repo.get_secret(FakeConn(row=None), "p1") is None
    assert repo.get_secret(FakeConn(row={"secret": "shh"}), "p1") == "shh"


def test_list_all_returns_every_row():
    conn = RowsConn(rows=[{"project_gid": "p1", "webhook_gid": "w1", "secret": "shh"}])
    assert repo.list_all(conn) == [{"project_gid": "p1", "webhook_gid": "w1", "secret": "shh"}]


def test_delete_removes_by_project():
    conn = FakeConn()
    repo.delete(conn, "p1")
    query, params = conn.executed[0]
    assert "DELETE FROM asana_webhooks" in query
    assert params == ("p1",)

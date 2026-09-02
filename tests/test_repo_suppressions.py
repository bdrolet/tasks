import json

from repo import suppressions
from tests.test_repo import FakeConn


def test_exists_true_when_row_found():
    conn = FakeConn(row={"exists": 1})
    assert suppressions.exists(conn, "m1") is True
    query, params = conn.executed[0]
    assert "SELECT 1 FROM suppressed_emails WHERE message_id" in query
    assert params == ("m1",)


def test_exists_false_when_no_row():
    conn = FakeConn(row=None)
    assert suppressions.exists(conn, "m1") is False


def test_insert_writes_all_columns_and_is_idempotent():
    conn = FakeConn()
    suppressions.insert(
        conn,
        message_id="m1",
        category="review",
        importance="P1",
        subject="Your bill",
        sender="billing@xfinity.com",
        reason="autopay already processed",
        source="agent",
        related_task_gid=None,
        evidence=[{"kind": "email", "ref": "m0", "note": "Thanks for your payment"}],
    )
    query, params = conn.executed[0]
    assert "INSERT INTO suppressed_emails" in query
    assert "ON CONFLICT (message_id) DO NOTHING" in query
    assert "%s::jsonb" in query
    assert params[:8] == (
        "m1",
        "review",
        "P1",
        "Your bill",
        "billing@xfinity.com",
        "autopay already processed",
        "agent",
        None,
    )
    assert json.loads(params[8]) == [
        {"kind": "email", "ref": "m0", "note": "Thanks for your payment"}
    ]

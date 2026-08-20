import json

from repo import suppressions
from tests.test_repo import FakeConn


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

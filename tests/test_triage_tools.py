import json

import clients.asana as asana
import clients.inbox_api as inbox_api
import clients.vertex as vertex
from repo import task_index as repo_index
from repo import tasks as repo_tasks
from services import triage
from tests.test_repo import FakeConn


def _j(tool, **kwargs):
    return json.loads(tool.call(kwargs))


# --- search_emails -----------------------------------------------------------

def test_search_emails_returns_trimmed_rows(monkeypatch):
    calls = []

    def fake_search(query, *, mode, limit, mailboxes=None):
        calls.append((query, mode, limit))
        return [
            {
                "message_id": "m9",
                "subject": "Thanks for your payment",
                "sender": "billing@xfinity.com",
                "sender_display": "Xfinity",
                "received_at": "2026-08-10T12:00:00Z",
                "preview": "Your automatic payment was processed",
                "web_link": "https://outlook/x",
                "mailbox": "me",
                "category": None,
                "importance": None,
            }
        ]

    monkeypatch.setattr(inbox_api, "search", fake_search)
    rows = _j(triage.search_emails, query="from:xfinity", limit=50)
    assert calls == [("from:xfinity", "graph", 25)]  # limit clamped to 25
    assert rows == [
        {
            "message_id": "m9",
            "subject": "Thanks for your payment",
            "sender": "billing@xfinity.com",
            "received_at": "2026-08-10T12:00:00Z",
            "preview": "Your automatic payment was processed",
            "category": None,
            "importance": None,
        }
    ]


def test_search_emails_error_is_returned_not_raised(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("inbox-api down")

    monkeypatch.setattr(inbox_api, "search", boom)
    assert _j(triage.search_emails, query="x") == {"error": "RuntimeError: inbox-api down"}


def test_search_emails_malformed_result_is_returned_not_raised(monkeypatch):
    monkeypatch.setattr(inbox_api, "search", lambda *a, **k: ["not-a-dict"])
    out = _j(triage.search_emails, query="x")
    assert "error" in out


# --- get_email ---------------------------------------------------------------

def test_get_email_returns_body_and_recipients(monkeypatch):
    monkeypatch.setattr(
        inbox_api,
        "get_email",
        lambda mid: {
            "id": mid,
            "subject": "Re: coaching",
            "from_email": "andrew@example.com",
            "from_name": "Andrew",
            "to": [{"name": "Ben", "address": "ben@drolet.cloud"}],
            "cc": [{"name": "Christy", "address": "christy@example.com"}],
            "received_at": "2026-08-14T16:16:00Z",
            "body": "<p>Ok thanks, Ben.</p>",
            "body_type": "html",
            "web_link": "https://outlook/m1",
        },
    )
    out = _j(triage.get_email, message_id="m1")
    assert out["subject"] == "Re: coaching"
    assert out["sender"] == "andrew@example.com"
    assert out["to"] == ["ben@drolet.cloud"]
    assert out["cc"] == ["christy@example.com"]
    assert out["body"] == "Ok thanks, Ben."  # html stripped


def test_get_email_malformed_recipients_is_returned_not_raised(monkeypatch):
    monkeypatch.setattr(
        inbox_api,
        "get_email",
        lambda mid: {"body": "hi", "body_type": "text", "to": ["not-a-dict"], "cc": []},
    )
    out = _j(triage.get_email, message_id="m1")
    assert "error" in out


def test_get_email_caps_body(monkeypatch):
    monkeypatch.setattr(
        inbox_api,
        "get_email",
        lambda mid: {"body": "x" * 5000, "body_type": "text", "to": [], "cc": []},
    )
    assert len(_j(triage.get_email, message_id="m1")["body"]) == triage.EMAIL_BODY_CAP


# --- search_tasks ------------------------------------------------------------

def _stub_conn(monkeypatch, rows, message_gid=None):
    conn = FakeConn()
    monkeypatch.setattr(triage, "get_conn", lambda: conn)
    monkeypatch.setattr(repo_index, "substring_candidates", lambda c, **kw: rows)
    monkeypatch.setattr(repo_index, "get_rows", lambda c, gids: [r for r in rows if r["task_gid"] in gids])
    monkeypatch.setattr(repo_tasks, "get_gid_by_message", lambda c, mid: message_gid)
    return conn


def test_search_tasks_substring_path(monkeypatch):
    _stub_conn(monkeypatch, [{"task_gid": "t1", "title": "Disney", "notes": "n", "completed": True,
                              "due_on": None, "project": "P", "permalink_url": "u"}])
    out = _j(triage.search_tasks, query="disney", completed=None)
    assert out == [{"task_gid": "t1", "title": "Disney", "notes": "n", "completed": True,
                    "due_on": None, "project": "P", "permalink_url": "u"}]


def test_search_tasks_semantic_path_attaches_scores(monkeypatch):
    rows = [{"task_gid": "t1", "title": "Refund", "notes": "", "completed": False,
             "due_on": None, "project": None, "permalink_url": None}]
    _stub_conn(monkeypatch, rows)
    monkeypatch.setattr(vertex, "embed", lambda text, *, task_type: [0.1] * 768)
    monkeypatch.setattr(repo_index, "semantic_candidates", lambda c, **kw: [{"task_gid": "t1", "score": 0.91}])
    out = _j(triage.search_tasks, query="disney refund", semantic=True)
    assert out[0]["task_gid"] == "t1" and out[0]["score"] == 0.91


def test_search_tasks_prepends_this_emails_existing_task(monkeypatch):
    rows = [{"task_gid": "t1", "title": "Other", "notes": "", "completed": False,
             "due_on": None, "project": None, "permalink_url": None},
            {"task_gid": "t0", "title": "Same thread", "notes": "", "completed": False,
             "due_on": None, "project": None, "permalink_url": None}]
    _stub_conn(monkeypatch, rows, message_gid="t0")
    token = triage.CURRENT_MESSAGE_ID.set("msg-123")
    try:
        out = _j(triage.search_tasks, query="zzz")
    finally:
        triage.CURRENT_MESSAGE_ID.reset(token)
    assert out[0]["task_gid"] == "t0" and out[0]["same_email"] is True
    assert [r["task_gid"] for r in out].count("t0") == 1


def test_search_tasks_error_is_returned(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(triage, "get_conn", boom)
    assert _j(triage.search_tasks, query="x") == {"error": "RuntimeError: db down"}


# --- get_task ----------------------------------------------------------------

def test_get_task_returns_detail_and_comments(monkeypatch):
    monkeypatch.setattr(
        asana,
        "get_task_detail",
        lambda gid: {"gid": gid, "name": "Follow up on refund", "notes": "N" * 1000,
                     "completed": True, "due_on": "2026-07-01", "permalink_url": "https://a/1"},
    )
    monkeypatch.setattr(
        asana,
        "get_stories",
        lambda gid: [
            {"type": "system", "text": "added to project", "created_at": "2026-06-01T00:00:00Z"},
            {"type": "comment", "text": "Refund landed", "created_at": "2026-07-02T00:00:00Z",
             "created_by": {"name": "Ben"}},
        ],
    )
    out = _j(triage.get_task, task_gid="1")
    assert out["name"] == "Follow up on refund"
    assert out["completed"] is True
    assert len(out["notes"]) == triage.TASK_NOTES_CAP
    assert out["comments"] == [{"text": "Refund landed", "created_at": "2026-07-02T00:00:00Z", "by": "Ben"}]


def test_get_task_missing(monkeypatch):
    monkeypatch.setattr(asana, "get_task_detail", lambda gid: None)
    assert _j(triage.get_task, task_gid="nope") == {"error": "not found"}


def test_get_task_malformed_stories_is_returned_not_raised(monkeypatch):
    monkeypatch.setattr(
        asana,
        "get_task_detail",
        lambda gid: {"gid": gid, "name": "Follow up", "notes": "", "completed": False,
                     "due_on": None, "permalink_url": None},
    )
    monkeypatch.setattr(asana, "get_stories", lambda gid: ["not-a-dict"])
    out = _j(triage.get_task, task_gid="1")
    assert "error" in out


def test_tool_calls_are_counted(monkeypatch):
    counts = []
    monkeypatch.setattr(triage.otel.triage_tool_calls, "add", lambda n, attrs: counts.append(attrs))
    monkeypatch.setattr(inbox_api, "search", lambda *a, **k: [])
    triage.search_emails.call({"query": "x"})
    assert counts == [{"tool": "search_emails"}]


def test_tools_list_and_names():
    assert [t.name for t in triage.TOOLS] == ["search_emails", "get_email", "search_tasks", "get_task"]

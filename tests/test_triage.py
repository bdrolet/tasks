import json

import clients.claude as claude
from models.events import Decision
from services import standing_context, triage
from tests.test_events import make_email_event


def _agent(monkeypatch, text=None, stop="end_turn", capture=None):
    def fake_run_agent(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        if isinstance(text, BaseException):
            raise text
        return text, stop

    monkeypatch.setattr(claude, "run_agent", fake_run_agent)


def _roles(monkeypatch, body):
    monkeypatch.setattr(
        standing_context, "section", lambda name, **kw: body if name == "Roles" else ""
    )


def _ok(actionable=False, reason="coach fact applies", gid=None, evidence=None):
    return json.dumps(
        {
            "actionable": actionable,
            "reason": reason,
            "related_task_gid": gid,
            "evidence": evidence if evidence is not None else [{"kind": "fact", "ref": "Assistant Coach", "note": "resigned"}],
        }
    )


def test_suppression_decision_parsed(monkeypatch):
    _roles(monkeypatch, "### Assistant Coach\nEnded 2026-08-14.")
    captured = {}
    _agent(monkeypatch, _ok(), capture=captured)
    d = triage.decide(make_email_event(), today="2026-08-19")
    assert d == Decision(
        actionable=False,
        reason="coach fact applies",
        related_task_gid=None,
        evidence=[{"kind": "fact", "ref": "Assistant Coach", "note": "resigned"}],
        outcome="suppressed",
    )
    assert captured["tools"] is triage.TOOLS
    assert captured["output_schema"] is triage.OUTPUT_SCHEMA
    assert captured["max_iterations"] == triage.MAX_ITERATIONS
    assert captured["deadline_s"] == triage.DEADLINE_S
    assert captured["system"] == triage.SYSTEM_PROMPT


def test_user_message_has_date_roles_and_email(monkeypatch):
    _roles(monkeypatch, "### Assistant Coach\nEnded 2026-08-14.")
    captured = {}
    _agent(monkeypatch, _ok(True, "new request"), capture=captured)
    ev = make_email_event(cc=["x@example.com"], body="B" * 5000)
    triage.decide(ev, today="2026-08-19")
    user = captured["user"]
    assert "Today is 2026-08-19" in user
    assert "### Assistant Coach" in user
    assert "Subject: Quarterly report" in user
    assert "From: alice@example.com" in user
    assert "To: ben@drolet.cloud" in user
    assert "Cc: x@example.com" in user
    assert "review / P1" in user
    assert user.count("B") >= triage.BODY_CAP and user.count("B") < 5000


def test_user_message_omits_roles_block_when_empty(monkeypatch):
    _roles(monkeypatch, "")
    captured = {}
    _agent(monkeypatch, _ok(True, "x"), capture=captured)
    triage.decide(make_email_event(), today="2026-08-19")
    assert "Standing facts" not in captured["user"]


def test_related_task_outcome(monkeypatch):
    _roles(monkeypatch, "")
    _agent(monkeypatch, _ok(False, "same refund", gid="1217290596630525"))
    d = triage.decide(make_email_event())
    assert d.outcome == "attached" and d.related_task_gid == "1217290596630525"


def test_actionable_outcome(monkeypatch):
    _roles(monkeypatch, "")
    _agent(monkeypatch, _ok(True, "a real request"))
    assert triage.decide(make_email_event()).outcome == "actionable"


def test_urgent_short_circuits_without_calling_agent(monkeypatch):
    called = []
    monkeypatch.setattr(claude, "run_agent", lambda **kw: called.append(1) or (None, "end_turn"))
    d = triage.decide(make_email_event(category="urgent"))
    assert d == Decision() and called == []


def test_fail_open_paths(monkeypatch):
    _roles(monkeypatch, "")
    cases = [
        (RuntimeError("boom"), "end_turn"),
        (None, "refusal"),
        (None, "max_iterations"),
        (None, "timeout"),
        ("not json", "end_turn"),
        (json.dumps({"actionable": "maybe"}), "end_turn"),
        (_ok(False, ""), "end_turn"),            # suppression with empty reason
        (_ok(False, "   "), "end_turn"),
    ]
    for text, stop in cases:
        _agent(monkeypatch, text, stop)
        d = triage.decide(make_email_event())
        assert d.actionable is True and d.outcome == "fail_open", (text, stop)


def test_current_message_id_is_set_during_run_and_cleared_after(monkeypatch):
    _roles(monkeypatch, "")
    seen = {}

    def fake_run_agent(**kwargs):
        seen["mid"] = triage.CURRENT_MESSAGE_ID.get()
        return _ok(True, "x"), "end_turn"

    monkeypatch.setattr(claude, "run_agent", fake_run_agent)
    triage.decide(make_email_event(message_id="msg-777"))
    assert seen["mid"] == "msg-777"
    assert triage.CURRENT_MESSAGE_ID.get() is None


def test_duration_metric_recorded(monkeypatch):
    _roles(monkeypatch, "")
    recorded = []
    monkeypatch.setattr(triage.otel.triage_duration, "record", lambda ms, attrs: recorded.append(attrs))
    _agent(monkeypatch, _ok(True, "x"))
    triage.decide(make_email_event())
    assert recorded == [{"outcome": "actionable"}]


def test_output_schema_is_strict_object():
    s = triage.OUTPUT_SCHEMA
    assert s["type"] == "object" and s["additionalProperties"] is False
    assert set(s["required"]) == {"actionable", "reason", "related_task_gid", "evidence"}


def test_system_prompt_carries_the_rules():
    p = triage.SYSTEM_PROMPT
    for needle in ("ONLY", "period", "cc", "no action required", "automatic payment",
                   "related_task_gid", "evidence", "Action required"):
        assert needle in p, needle

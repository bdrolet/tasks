import json

import clients.claude as claude
import clients.inbox_api as inbox_api
from services import screening, standing_context
from tests.test_events import make_email_event


def _attachments(monkeypatch, payload):
    def fake(gid):
        if isinstance(payload, BaseException):
            raise payload
        return payload

    monkeypatch.setattr(inbox_api, "get_attachments", fake)


def test_attachment_lines_skipped_when_flag_absent(monkeypatch):
    _attachments(monkeypatch, AssertionError("must not be called"))
    assert screening.attachment_lines(make_email_event()) == []


def test_attachment_lines_formats_name_type_size(monkeypatch):
    _attachments(
        monkeypatch,
        {
            "attachments": [
                {"name": "Checking.csv", "content_type": "text/csv", "size": 65903},
                {"name": "logo.png", "content_type": "image/png", "size": 40, "is_inline": True},
            ]
        },
    )
    event = make_email_event(has_attachments=True, graph_message_id="AAMk")
    assert screening.attachment_lines(event) == ["Checking.csv  text/csv  64.4 KB"]


def test_attachment_lines_uses_graph_id_not_message_id(monkeypatch):
    seen = []
    monkeypatch.setattr(inbox_api, "get_attachments", lambda gid: seen.append(gid) or {})
    screening.attachment_lines(
        make_email_event(message_id="uuid-form", graph_message_id="AAMkGRAPH", has_attachments=True)
    )
    assert seen == ["AAMkGRAPH"]


def test_attachment_lines_missing_graph_id_is_not_an_error(monkeypatch):
    _attachments(monkeypatch, AssertionError("must not be called"))
    assert screening.attachment_lines(make_email_event(has_attachments=True)) == []


def test_attachment_lines_swallows_backend_failure(monkeypatch):
    _attachments(monkeypatch, RuntimeError("inbox-api down"))
    event = make_email_event(has_attachments=True, graph_message_id="AAMk")
    assert screening.attachment_lines(event) == []


def test_build_user_message_includes_facts_headers_and_attachments():
    event = make_email_event(subject="checking and saving", body="")
    msg = screening.build_user_message(
        event,
        today="2026-08-24",
        roles="Ben handles Dana's finances.",
        attachments=["Checking.csv  text/csv  64.4 KB"],
    )
    assert "Today is 2026-08-24." in msg
    assert "Ben handles Dana's finances." in msg
    assert "Subject: checking and saving" in msg
    assert "Attachments:" in msg
    assert "Checking.csv  text/csv  64.4 KB" in msg
    assert "(empty body)" in msg


def test_build_user_message_caps_body():
    event = make_email_event(body="x" * 5000)
    msg = screening.build_user_message(event, today="2026-08-24", roles="", attachments=[])
    assert "x" * screening.BODY_CAP in msg
    assert "x" * (screening.BODY_CAP + 1) not in msg


def test_output_schema_is_three_way():
    assert screening.VERDICTS == ("task", "relate", "drop")
    assert screening.OUTPUT_SCHEMA["properties"]["verdict"]["enum"] == ["task", "relate", "drop"]


def _classify(monkeypatch, payload, capture=None):
    def fake(*, system, user, schema):
        if capture is not None:
            capture.update(system=system, user=user, schema=schema)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    monkeypatch.setattr(claude, "classify", fake)


def _no_attachments(monkeypatch):
    monkeypatch.setattr(screening, "attachment_lines", lambda event: [])


def _roles(monkeypatch, body=""):
    monkeypatch.setattr(
        standing_context, "section", lambda name, **kw: body if name == "Roles" else ""
    )


def _verdict(verdict="task", priority="P1", reason="because"):
    return json.dumps({"verdict": verdict, "priority": priority, "reason": reason})


def test_screen_accepts_a_task_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("task", "P1", "bank statements attached"))
    result = screening.screen(make_email_event(category="ignore"), today="2026-08-24")
    assert result.verdict == "task"
    assert result.is_task is True
    assert result.priority == "P1"
    assert result.reason == "bank statements attached"
    assert result.outcome == "task"


def test_screen_accepts_a_relate_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("relate", "P1", "rental car confirmation"))
    result = screening.screen(make_email_event(category="reference"), today="2026-08-27")
    assert result.verdict == "relate"
    assert result.is_task is False
    assert result.outcome == "relate"


def test_screen_accepts_a_drop_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("drop", "P3", "marketing newsletter"))
    result = screening.screen(make_email_event(), today="2026-08-24")
    assert result.verdict == "drop"
    assert result.outcome == "drop"


def test_screen_passes_facts_and_email_to_the_model(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch, "Ben handles Dana's finances.")
    captured = {}
    _classify(monkeypatch, _verdict(), capture=captured)
    screening.screen(make_email_event(subject="checking and saving"), today="2026-08-24")
    assert captured["system"] == screening.SYSTEM_PROMPT
    assert captured["schema"] == screening.OUTPUT_SCHEMA
    assert "Ben handles Dana's finances." in captured["user"]
    assert "checking and saving" in captured["user"]


def test_screen_falls_back_to_warrants_task_on_api_failure(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, RuntimeError("anthropic down"))

    rescued = screening.screen(make_email_event(category="review", importance="P1"))
    assert rescued.verdict == "task"
    assert rescued.priority == "P1"
    assert rescued.outcome == "fail_open"

    dropped = screening.screen(make_email_event(category="ignore", importance="P3"))
    assert dropped.verdict == "drop"
    assert dropped.outcome == "fail_open"


def test_fallback_never_returns_relate(monkeypatch):
    """relate is a judgement the fallback cannot make; it must not guess one."""
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, RuntimeError("down"))
    verdicts = [
        screening.screen(make_email_event(category=c)).verdict
        for c in ("urgent", "review", "respond", "reference", "ignore")
    ]
    assert verdicts == ["task", "task", "task", "drop", "drop"]


def test_screen_falls_back_on_unparseable_output(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, "not json at all")
    result = screening.screen(make_email_event(category="ignore"))
    assert result.outcome == "fail_open"
    assert result.verdict == "drop"


def test_screen_falls_back_on_an_unknown_verdict(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("maybe", "P1", "x"))
    assert screening.screen(make_email_event(category="ignore")).outcome == "fail_open"


def test_screen_coerces_an_unknown_priority(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, _verdict("task", "URGENT", "x"))
    assert screening.screen(make_email_event()).priority == "P2"


def test_screen_fallback_coerces_a_bad_inbox_importance(monkeypatch):
    _no_attachments(monkeypatch)
    _roles(monkeypatch)
    _classify(monkeypatch, RuntimeError("down"))
    result = screening.screen(make_email_event(category="review", importance="high"))
    assert result.priority == "P2"


# --- audience (shared-with-Cheryl) -------------------------------------------


def _facts(monkeypatch, roles="", routing=""):
    monkeypatch.setattr(
        standing_context,
        "section",
        lambda name, **kw: {"Roles": roles, "Calendar Routing": routing}.get(name, ""),
    )


def _verdict_with_audience(audience):
    return json.dumps(
        {"verdict": "task", "priority": "P1", "reason": "because", "audience": audience}
    )


def test_output_schema_has_a_two_way_audience():
    props = screening.OUTPUT_SCHEMA["properties"]
    assert props["audience"]["enum"] == ["self", "shared"]
    assert "audience" in screening.OUTPUT_SCHEMA["required"]


def test_screen_reads_a_shared_audience(monkeypatch):
    _no_attachments(monkeypatch)
    _facts(monkeypatch)
    _classify(monkeypatch, _verdict_with_audience("shared"))
    result = screening.screen(make_email_event(), today="2026-09-04")
    assert result.audience == "shared"


def test_screen_defaults_audience_to_self_when_missing_or_unknown(monkeypatch):
    _no_attachments(monkeypatch)
    _facts(monkeypatch)
    _classify(monkeypatch, _verdict("task", "P1", "x"))
    assert screening.screen(make_email_event(), today="2026-09-04").audience == "self"
    _classify(monkeypatch, _verdict_with_audience("everyone"))
    assert screening.screen(make_email_event(), today="2026-09-04").audience == "self"


def test_fallback_audience_is_self(monkeypatch):
    _no_attachments(monkeypatch)
    _facts(monkeypatch)
    _classify(monkeypatch, RuntimeError("anthropic down"))
    assert screening.screen(make_email_event(), today="2026-09-04").audience == "self"


def test_screen_passes_household_facts_to_the_model(monkeypatch):
    _no_attachments(monkeypatch)
    _facts(monkeypatch, roles="Ben is treasurer.", routing="Household is Ben and his partner.")
    captured = {}
    _classify(monkeypatch, _verdict(), capture=captured)
    screening.screen(make_email_event(), today="2026-09-04")
    assert "Ben is treasurer." in captured["user"]
    assert "Household is Ben and his partner." in captured["user"]


def test_build_user_message_omits_household_block_when_empty():
    msg = screening.build_user_message(
        make_email_event(), today="2026-09-04", roles="", routing="", attachments=[]
    )
    assert "Household" not in msg

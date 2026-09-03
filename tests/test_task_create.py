import pytest

import clients.asana as asana
import clients.otel  # noqa: F401 — imported by handler
from handlers import task_create
from models.events import CreatedTask, Decision, EmailSummary
from repo import suppressions as repo_suppressions
from repo import tasks as repo_tasks
from services import deadline, email_summary, tags, triage
from tests.test_events import make_email_event
from tests.test_repo import FakeConn


@pytest.fixture(autouse=True)
def _default_triage(monkeypatch):
    monkeypatch.setattr(triage, "decide", lambda event, **kw: Decision())


@pytest.fixture(autouse=True)
def _default_screening(monkeypatch):
    from models.events import Screening
    from services import screening

    monkeypatch.setattr(screening, "screen", lambda event, **kw: Screening(priority="P1"))


def _stub_db(monkeypatch):
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    inserts = []
    monkeypatch.setattr(repo_tasks, "insert", lambda conn, **kw: inserts.append(kw))
    return inserts


def _stub_enrichment(monkeypatch, key_points=None, links=None, due=None, title=None):
    summary_calls = []

    def fake_generate(event):
        summary_calls.append(event)
        return EmailSummary(key_points=key_points or [], relevant_links=links or [], title=title)

    monkeypatch.setattr(email_summary, "generate", fake_generate)
    deadline_calls = []

    def fake_deadline(event):
        deadline_calls.append(event)
        return due

    monkeypatch.setattr(deadline, "extract_deadline", fake_deadline)
    return summary_calls, deadline_calls


def _capture_create(monkeypatch, result="42"):
    created = {}

    def fake_create(event, *, tag_gids=None, due_date=None, html_notes="", title=None):
        created.update(tag_gids=tag_gids, due_date=due_date, html_notes=html_notes, title=title)
        if result is None:
            return None
        return CreatedTask(gid=result, permalink_url=f"https://a/{result}")

    monkeypatch.setattr(asana, "create_task", fake_create)
    return created


def _screen_as(monkeypatch, **kwargs):
    from models.events import Screening
    from services import screening

    verdict = Screening(**kwargs)
    monkeypatch.setattr(screening, "screen", lambda event, **kw: verdict)
    return verdict


def _relate_as(monkeypatch, **kwargs):
    from models.events import Match
    from services import relating

    result = Match(**kwargs)
    monkeypatch.setattr(relating, "match", lambda event: result)
    return result


def test_handle_enriches_creates_places_and_stores(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: ["tg1"])
    inserts = _stub_db(monkeypatch)
    _stub_enrichment(
        monkeypatch, key_points=["Summarized point"], links=[["https://x", "Doc"]], due="2026-07-31"
    )
    created = _capture_create(monkeypatch)
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    event = make_email_event(
        seed_key_points=["Calendar invite: Standup"], seed_links=[["https://z", "RSVP: Accept"]]
    )
    task_create.handle(event)

    assert created["tag_gids"] == ["tg1"]
    # merged summary + seed key points render into the description
    assert "<li>Summarized point</li>" in created["html_notes"]
    assert "<li>Calendar invite: Standup</li>" in created["html_notes"]
    assert '<a href="https://x">Doc</a>' in created["html_notes"]
    assert '<a href="https://z">RSVP: Accept</a>' in created["html_notes"]
    assert created["due_date"] == "2026-07-31"  # P1 → deadline extraction ran
    assert moves == [("42", "sec-review")]
    assert inserts == [
        {"task_gid": "42", "message_id": "msg-123", "category": "review", "importance": "P1"}
    ]


def test_handle_passes_enriched_title(monkeypatch):
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch, title="Review Q3 board deck")
    created = _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)

    task_create.handle(make_email_event(importance="P1"))

    assert created["title"] == "[P1] Review Q3 board deck"


def test_handle_skips_enrichment_when_the_screener_drops(monkeypatch):
    _screen_as(monkeypatch, verdict="drop", reason="marketing newsletter", outcome="drop")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: None)
    summary_calls, _ = _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(category="ignore"))
    task_create.handle(make_email_event(category="reference"))

    assert summary_calls == []  # gate 1 runs BEFORE enrichment — no Claude spend
    assert created == {}


def test_deadline_extraction_only_for_p0_p1(monkeypatch):
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _, deadline_calls = _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)

    _screen_as(monkeypatch, priority="P2")
    task_create.handle(make_email_event())
    assert deadline_calls == []

    _screen_as(monkeypatch, priority="P0")
    task_create.handle(make_email_event())
    assert len(deadline_calls) == 1


def test_handle_duplicate_skips_move_and_store(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    inserts = _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch, result=None)
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_create.handle(make_email_event())
    assert moves == []
    assert inserts == []


def test_handle_db_failure_does_not_block_section_move(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch)
    monkeypatch.setattr(
        task_create, "get_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    moves = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: moves.append((t, s)))

    task_create.handle(make_email_event())  # must not raise
    assert moves == [("42", "sec-review")]


def test_created_task_is_indexed(monkeypatch):
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)
    refreshed = []
    monkeypatch.setattr(task_create.task_index, "refresh", refreshed.append)

    task_create.handle(make_email_event())
    assert refreshed == ["42"]


def test_no_task_means_no_index_refresh(monkeypatch):
    refreshed = []
    monkeypatch.setattr(task_create.task_index, "refresh", refreshed.append)
    _screen_as(monkeypatch, verdict="drop", reason="policy gate rejects", outcome="drop")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: None)

    task_create.handle(make_email_event())
    assert refreshed == []


def _stub_triage(monkeypatch, decision):
    calls = []

    def fake_decide(event, **kw):
        calls.append(event)
        return decision

    monkeypatch.setattr(triage, "decide", fake_decide)
    return calls


def _stub_suppressions(monkeypatch):
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    return rows


def _stub_story(monkeypatch, fail=False):
    stories = []

    def fake_story(task_gid, *, text=None, html_text=None):
        if fail:
            raise RuntimeError("asana down")
        stories.append((task_gid, text))
        return {"gid": "s1"}

    monkeypatch.setattr(asana, "create_story", fake_story)
    return stories


def _count_suppressed(monkeypatch):
    counts = []
    monkeypatch.setattr(
        task_create.otel.tasks_suppressed, "add", lambda n, attrs: counts.append(attrs)
    )
    return counts


def test_suppressed_decision_creates_nothing_and_records(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    summary_calls, _ = _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    _stub_triage(
        monkeypatch,
        Decision(
            actionable=False,
            reason="coach fact",
            evidence=[{"kind": "fact", "ref": "Coach", "note": "x"}],
            outcome="suppressed",
        ),
    )
    task_create.handle(make_email_event())
    assert created == {} and summary_calls == []
    assert rows == [
        {
            "message_id": "msg-123",
            "category": "review",
            "importance": "P1",
            "subject": "Quarterly report",
            "sender": "alice@example.com",
            "reason": "coach fact",
            "source": "agent",
            "related_task_gid": None,
            "evidence": [{"kind": "fact", "ref": "Coach", "note": "x"}],
        }
    ]
    assert counts == [
        {
            "category": "review",
            "importance": "P1",
            "source": "agent",
            "attached": "false",
            "resolves": "false",
        }
    ]


def test_attached_decision_comments_on_related_task(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    stories = _stub_story(monkeypatch)
    _stub_triage(
        monkeypatch,
        Decision(actionable=False, reason="same refund", related_task_gid="t9", outcome="attached"),
    )
    task_create.handle(make_email_event())
    assert created == {}
    assert stories == [
        ("t9", "Related email: Quarterly report — same refund — https://outlook.example/msg-123")
    ]
    assert rows[0]["related_task_gid"] == "t9" and rows[0]["source"] == "agent"
    assert counts[0]["attached"] == "true"


def test_resolving_decision_asks_ben_to_close(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    stories = _stub_story(monkeypatch)
    _stub_triage(
        monkeypatch,
        Decision(
            actionable=False,
            reason="refund landed",
            related_task_gid="t9",
            resolves=True,
            outcome="attached",
        ),
    )
    task_create.handle(make_email_event())
    assert created == {}
    assert stories == [
        (
            "t9",
            "Looks resolved — close this task if you agree. Quarterly report"
            " — refund landed — https://outlook.example/msg-123",
        )
    ]
    assert counts[0]["resolves"] == "true"
    assert rows[0]["related_task_gid"] == "t9"


def test_non_resolving_attachment_keeps_the_plain_lead(monkeypatch):
    _stub_db(monkeypatch)
    _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch)
    stories = _stub_story(monkeypatch)
    _stub_triage(
        monkeypatch,
        Decision(actionable=False, reason="same saga", related_task_gid="t9", outcome="attached"),
    )
    task_create.handle(make_email_event())
    assert stories[0][1].startswith("Related email:")
    assert "close this task" not in stories[0][1]
    assert counts[0]["resolves"] == "false"


def test_story_failure_does_not_resurrect_task(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    _stub_story(monkeypatch, fail=True)
    _stub_triage(
        monkeypatch,
        Decision(actionable=False, reason="r", related_task_gid="t9", outcome="attached"),
    )
    task_create.handle(make_email_event())
    assert created == {} and len(rows) == 1


def test_suppression_row_failure_does_not_resurrect_task(monkeypatch):
    _stub_db(monkeypatch)

    def boom(conn, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(repo_suppressions, "insert", boom)
    _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)
    _stub_triage(monkeypatch, Decision(actionable=False, reason="r", outcome="suppressed"))
    task_create.handle(make_email_event())  # must not raise
    assert created == {}


def test_actionable_decision_proceeds_unchanged(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    summary_calls, _ = _stub_enrichment(monkeypatch, key_points=["Pay the bill"])
    created = _capture_create(monkeypatch)
    calls = _stub_triage(monkeypatch, Decision(actionable=True, reason="new", outcome="actionable"))
    task_create.handle(make_email_event())
    assert len(calls) == 1 and len(summary_calls) == 1
    assert created["html_notes"] and rows == []


def test_phrase_veto_after_summary(monkeypatch):
    _stub_db(monkeypatch)
    rows = _stub_suppressions(monkeypatch)
    counts = _count_suppressed(monkeypatch)
    _stub_enrichment(monkeypatch, key_points=["Final payment will be sent; no action required"])
    created = _capture_create(monkeypatch)
    _stub_triage(monkeypatch, Decision(actionable=True, reason="looked fine", outcome="actionable"))
    task_create.handle(make_email_event())
    assert created == {}
    assert (
        rows[0]["source"] == "phrase"
        and rows[0]["reason"] == "Final payment will be sent; no action required"
    )
    assert counts[0]["source"] == "phrase"


def test_gate1_still_runs_before_triage(monkeypatch):
    calls = _stub_triage(monkeypatch, Decision())
    _screen_as(monkeypatch, verdict="drop", reason="reference material", outcome="drop")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: None)
    created = _capture_create(monkeypatch)
    task_create.handle(make_email_event(category="reference"))
    assert calls == [] and created == {}


def test_drop_verdict_suppresses_and_records(monkeypatch):
    _screen_as(monkeypatch, verdict="drop", reason="marketing newsletter", outcome="drop")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(category="ignore"))

    assert created == {}
    assert rows[0]["source"] == "screen"
    assert rows[0]["reason"] == "marketing newsletter"
    assert rows[0]["related_task_gid"] is None


def test_relate_verdict_comments_on_the_matched_task(monkeypatch):
    """The Enterprise case: a confirmation settles an open task."""
    _screen_as(monkeypatch, verdict="relate", reason="rental confirmation", outcome="relate")
    _relate_as(
        monkeypatch,
        task_gid="1217730397662201",
        resolves=True,
        reason="same reservation number",
        evidence=[{"kind": "task", "ref": "1217730397662201", "note": "same reservation"}],
    )
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    stories = []
    monkeypatch.setattr(asana, "create_story", lambda gid, text: stories.append((gid, text)))
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(category="reference"))

    assert created == {}
    assert stories[0][0] == "1217730397662201"
    assert "Looks resolved" in stories[0][1]
    assert rows[0]["source"] == "relate"
    assert rows[0]["related_task_gid"] == "1217730397662201"


def test_relate_with_no_match_still_records(monkeypatch):
    _screen_as(monkeypatch, verdict="relate", reason="delivery notice", outcome="relate")
    _relate_as(monkeypatch, reason="no open task above the similarity floor")
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    rows = []
    monkeypatch.setattr(repo_suppressions, "insert", lambda conn, **kw: rows.append(kw))
    stories = []
    monkeypatch.setattr(asana, "create_story", lambda gid, text: stories.append(gid))

    task_create.handle(make_email_event(category="ignore"))

    assert stories == []
    assert rows[0]["source"] == "relate"
    assert rows[0]["related_task_gid"] is None
    assert rows[0]["reason"] == "no open task above the similarity floor"


def test_duplicate_redelivery_skips_the_comment_but_still_records(monkeypatch):
    """Pub/Sub redelivery: suppressed_emails already has this message_id (an
    earlier delivery got there first), so the comment must not be posted a
    second time — but the (idempotent) insert still runs."""
    _screen_as(monkeypatch, verdict="relate", reason="rental confirmation", outcome="relate")
    _relate_as(monkeypatch, task_gid="t9", reason="same reservation number", resolves=False)
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn(row={"exists": 1}))
    stories = _stub_story(monkeypatch)

    task_create.handle(make_email_event())

    assert stories == []  # already recorded — no duplicate write to Asana


def test_existence_check_failure_falls_back_to_posting_the_comment(monkeypatch):
    """Ruling: if the existence check itself fails, behave exactly as if the
    guard did not exist — post the comment. The guard may only ever PREVENT
    a duplicate, never introduce a new way to lose one."""
    _screen_as(monkeypatch, verdict="relate", reason="rental confirmation", outcome="relate")
    _relate_as(monkeypatch, task_gid="t9", reason="same reservation number", resolves=False)
    calls = {"n": 0}

    def flaky_get_conn():
        calls["n"] += 1
        if calls["n"] == 1:  # the existence check
            raise RuntimeError("db down")
        return FakeConn()  # the later suppressed_emails insert

    monkeypatch.setattr(task_create, "get_conn", flaky_get_conn)
    stories = _stub_story(monkeypatch)

    task_create.handle(make_email_event())  # must not raise

    assert stories[0][0] == "t9"


def test_existence_check_never_raises_into_handle(monkeypatch):
    """Even if EVERY db call fails (check and insert both), handle() must
    not raise, and the comment must still be posted (fail-open guard)."""
    _screen_as(monkeypatch, verdict="relate", reason="rental confirmation", outcome="relate")
    _relate_as(monkeypatch, task_gid="t9", reason="same reservation number", resolves=False)
    monkeypatch.setattr(
        task_create, "get_conn", lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    )
    stories = _stub_story(monkeypatch)

    task_create.handle(make_email_event())  # must not raise

    assert stories[0][0] == "t9"


def test_screener_rescues_an_ignore_email(monkeypatch):
    """The Dana case: inbox said ignore/P3, the screener says task/P1."""
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    _screen_as(monkeypatch, verdict="task", priority="P1", reason="bank statements attached")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    inserts = _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch, due="2026-09-01", title="review Dana's bank statements")
    created = _capture_create(monkeypatch)
    placed = []
    monkeypatch.setattr(asana, "add_task_to_section", lambda gid, sec: placed.append(sec))

    task_create.handle(make_email_event(category="ignore", importance="P3"))

    assert created["title"] == "[P1] review Dana's bank statements"
    assert created["due_date"] == "2026-09-01"  # P1 → deadline extraction ran
    assert inserts[0]["importance"] == "P1"  # screener priority, not inbox's P3
    assert placed == ["sec-review"]  # rescued → Review, not unsectioned


def test_deadline_extraction_follows_screener_priority(monkeypatch):
    _screen_as(monkeypatch, priority="P3")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _, deadline_calls = _stub_enrichment(monkeypatch, due="2026-09-01", title="do a thing")
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(importance="P0"))

    assert deadline_calls == []  # inbox said P0; the screener says P3
    assert created["due_date"] is None


def test_title_falls_back_to_subject_with_screener_priority(monkeypatch):
    _screen_as(monkeypatch, priority="P2")
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _stub_enrichment(monkeypatch, title=None)
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(subject="Quarterly report"))

    assert created["title"] == "[P2] Quarterly report"

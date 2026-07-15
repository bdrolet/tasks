import clients.asana as asana
import clients.otel  # noqa: F401 — imported by handler
from handlers import task_create
from models.events import CreatedTask, EmailSummary
from repo import tasks as repo_tasks
from services import deadline, email_summary, tags
from tests.test_events import make_email_event
from tests.test_repo import FakeConn


def _stub_db(monkeypatch):
    monkeypatch.setattr(task_create, "get_conn", lambda: FakeConn())
    inserts = []
    monkeypatch.setattr(repo_tasks, "insert", lambda conn, **kw: inserts.append(kw))
    return inserts


def _stub_enrichment(monkeypatch, key_points=None, links=None, due=None):
    summary_calls = []

    def fake_generate(event):
        summary_calls.append(event)
        return EmailSummary(key_points=key_points or [], relevant_links=links or [])

    monkeypatch.setattr(email_summary, "generate", fake_generate)
    deadline_calls = []

    def fake_deadline(event):
        deadline_calls.append(event)
        return due

    monkeypatch.setattr(deadline, "extract_deadline", fake_deadline)
    return summary_calls, deadline_calls


def _capture_create(monkeypatch, result="42"):
    created = {}

    def fake_create(event, *, tag_gids=None, key_points=None, relevant_links=None, due_date=None):
        created.update(
            tag_gids=tag_gids,
            key_points=key_points,
            relevant_links=relevant_links,
            due_date=due_date,
        )
        if result is None:
            return None
        return CreatedTask(gid=result, permalink_url=f"https://a/{result}")

    monkeypatch.setattr(asana, "create_task", fake_create)
    return created


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
    assert created["key_points"] == ["Summarized point", "Calendar invite: Standup"]
    assert created["relevant_links"] == [["https://x", "Doc"], ["https://z", "RSVP: Accept"]]
    assert created["due_date"] == "2026-07-31"  # P1 → deadline extraction ran
    assert moves == [("42", "sec-review")]
    assert inserts == [
        {"task_gid": "42", "message_id": "msg-123", "category": "review", "importance": "P1"}
    ]


def test_handle_skips_non_task_categories_without_enrichment(monkeypatch):
    summary_calls, _ = _stub_enrichment(monkeypatch)
    created = _capture_create(monkeypatch)

    task_create.handle(make_email_event(category="ignore"))
    task_create.handle(make_email_event(category="reference"))

    assert summary_calls == []  # policy gate runs BEFORE enrichment — no Claude spend
    assert created == {}


def test_deadline_extraction_only_for_p0_p1(monkeypatch):
    monkeypatch.setattr(tags, "resolve_gids", lambda names: [])
    _stub_db(monkeypatch)
    _, deadline_calls = _stub_enrichment(monkeypatch)
    _capture_create(monkeypatch)
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: None)

    task_create.handle(make_email_event(importance="P2"))
    assert deadline_calls == []

    task_create.handle(make_email_event(importance="P0"))
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

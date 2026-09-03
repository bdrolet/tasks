import json

import clients.asana as asana
import clients.claude as claude
import clients.vertex as vertex
from models.events import Match
from repo import task_index as repo_index
from services import relating
from tests.test_events import make_email_event
from tests.test_repo import FakeConn


def _stub_corpus(monkeypatch, rows, embed_error=None):
    """rows: list of (task_gid, title, score)."""

    def fake_embed(text, *, task_type):
        if embed_error:
            raise embed_error
        return [0.1] * 768

    monkeypatch.setattr(vertex, "embed", fake_embed)
    monkeypatch.setattr(relating, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        repo_index,
        "semantic_candidates",
        lambda conn, **kw: [{"task_gid": g, "score": s} for g, _, s in rows],
    )
    monkeypatch.setattr(
        repo_index,
        "get_rows",
        lambda conn, gids: [
            {"task_gid": g, "title": t, "notes": "", "due_on": None}
            for g, t, _ in rows
            if g in gids
        ],
    )


def _stub_confirm(monkeypatch, payload, capture=None):
    def fake(*, system, user, schema):
        if capture is not None:
            capture.update(system=system, user=user, schema=schema)
        if isinstance(payload, BaseException):
            raise payload
        return payload

    monkeypatch.setattr(claude, "classify", fake)


def _answer(task_gid=None, resolves=False, reason="x"):
    return json.dumps({"task_gid": task_gid, "resolves": resolves, "reason": reason})


def test_match_below_the_floor_never_calls_the_model(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car", relating.SIMILARITY_FLOOR - 0.01)])
    _stub_confirm(monkeypatch, AssertionError("must not be called"))
    result = relating.match(make_email_event())
    assert result == Match(reason="no open task above the similarity floor")


def test_match_with_no_candidates_at_all(monkeypatch):
    _stub_corpus(monkeypatch, [])
    _stub_confirm(monkeypatch, AssertionError("must not be called"))
    assert relating.match(make_email_event()).task_gid is None


def test_match_confirms_and_verifies(monkeypatch):
    _stub_corpus(monkeypatch, [("1217730397662201", "Book rental car for Ohio trip", 0.83)])
    _stub_confirm(monkeypatch, _answer("1217730397662201", True, "same reservation"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)

    result = relating.match(make_email_event(subject="Confirmed: Enterprise Reservation 12345678"))

    assert result.task_gid == "1217730397662201"
    assert result.resolves is True
    assert result.reason == "same reservation"
    assert result.evidence == [
        {"kind": "task", "ref": "1217730397662201", "note": "same reservation"}
    ]


def test_null_is_a_normal_answer(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Something adjacent", 0.72)])
    _stub_confirm(monkeypatch, _answer(None, False, "only adjacent"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)
    result = relating.match(make_email_event())
    assert result.task_gid is None
    assert result.reason == "only adjacent"


def test_unfetchable_gid_is_treated_as_no_match(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car", 0.9)])
    _stub_confirm(monkeypatch, _answer("111", True, "same matter"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: False)
    assert relating.match(make_email_event()).task_gid is None


def test_a_gid_outside_the_candidates_is_rejected(monkeypatch):
    """Guards against a hallucinated gid landing a comment on an unrelated task."""
    _stub_corpus(monkeypatch, [("111", "Book rental car", 0.9)])
    _stub_confirm(monkeypatch, _answer("999999", True, "made up"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)
    assert relating.match(make_email_event()).task_gid is None


def test_candidate_titles_reach_the_confirm_prompt(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car for Ohio trip", 0.83)])
    captured = {}
    _stub_confirm(monkeypatch, _answer(None), capture=captured)
    relating.match(make_email_event(subject="Confirmed: Enterprise Reservation"))
    assert "Book rental car for Ohio trip" in captured["user"]
    assert "111" in captured["user"]
    assert "Confirmed: Enterprise Reservation" in captured["user"]


def test_embedding_failure_degrades_to_no_match(monkeypatch):
    _stub_corpus(monkeypatch, [], embed_error=RuntimeError("vertex down"))
    _stub_confirm(monkeypatch, AssertionError("must not be called"))
    assert relating.match(make_email_event()).task_gid is None


def test_confirm_failure_degrades_to_no_match(monkeypatch):
    _stub_corpus(monkeypatch, [("111", "Book rental car", 0.9)])
    _stub_confirm(monkeypatch, RuntimeError("anthropic down"))
    assert relating.match(make_email_event()).task_gid is None


def test_match_with_rows_skips_the_candidate_lookup(monkeypatch):
    """The offline dry run fetches candidates itself and passes them in;
    match() must use exactly those rows and never call _candidates again."""

    def boom(event):
        raise AssertionError("must not fetch candidates when rows is given")

    monkeypatch.setattr(relating, "_candidates", boom)
    _stub_confirm(monkeypatch, _answer("111", True, "same reservation"))
    monkeypatch.setattr(asana, "task_exists", lambda gid: True)

    rows = [
        {"task_gid": "111", "title": "Book rental car", "notes": "", "due_on": None, "score": 0.9}
    ]
    result = relating.match(make_email_event(), rows=rows)

    assert result.task_gid == "111"
    assert result.resolves is True


def test_match_with_empty_rows_is_below_floor_without_refetching(monkeypatch):
    """rows=[] means the caller already tried and found nothing — it must be
    treated as a below-floor no-match, not as "not supplied" (which would
    silently re-fetch on exactly the rows where the lookup already failed)."""

    def boom(event):
        raise AssertionError("must not fetch candidates when rows=[] is given")

    monkeypatch.setattr(relating, "_candidates", boom)
    _stub_confirm(monkeypatch, AssertionError("must not be called"))

    result = relating.match(make_email_event(), rows=[])

    assert result == Match(reason="no open task above the similarity floor")


def test_match_only_searches_open_tasks(monkeypatch):
    captured = {}
    monkeypatch.setattr(vertex, "embed", lambda text, *, task_type: [0.1] * 768)
    monkeypatch.setattr(relating, "get_conn", lambda: FakeConn())
    monkeypatch.setattr(
        repo_index, "semantic_candidates", lambda conn, **kw: captured.update(kw) or []
    )
    monkeypatch.setattr(repo_index, "get_rows", lambda conn, gids: [])
    relating.match(make_email_event())
    assert captured["completed"] is False
    assert captured["limit"] == relating.CANDIDATES

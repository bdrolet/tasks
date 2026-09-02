"""scripts/backtest_screening.py is a local-only script (needs GCP + inbox DB
access to run end to end), so it keeps every heavy import lazy and inside
functions — that also makes it importable in tests via its file path, with
services.relating monkeypatched underneath.

This file covers the harness defect fixed in Task 12 — `_relate()` must
compute the candidate rows ONCE per relate row and derive both the neighbour
diagnostics and the verdict from that single lookup — and Task 13's follow-up:
`_relate()` now gets the verdict by calling relating.match(event, rows=rows)
instead of hand-copying match()'s floor-then-confirm control flow, so these
tests (which monkeypatch relating._candidates/_confirm and assert on
backtest._relate's return value) are also the regression guard that the
collapse onto match() didn't change the harness's observable behaviour.
services/relating.py's own unit tests (tests/test_relating.py) are the proof
that adding the `rows` parameter left production's call site bit-identical.
This file also covers that the harness's progress output goes to stderr, not
stdout, and carries no email content — stdout carries the summary table that
gets pasted into review."""

import argparse
import csv
import importlib.util
import pathlib

import services.relating as relating
from models.events import Match, Screening
from tests.test_events import make_email_event

_SPEC = importlib.util.spec_from_file_location(
    "backtest_screening",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "backtest_screening.py",
)
backtest = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backtest)


def test_relate_calls_candidates_exactly_once(monkeypatch):
    calls = []

    def fake_candidates(event):
        calls.append(event)
        return [{"task_gid": "111", "title": "Book rental car", "score": 0.9}]

    monkeypatch.setattr(relating, "_candidates", fake_candidates)
    monkeypatch.setattr(relating, "_confirm", lambda event, rows: Match(reason="no match"))

    backtest._relate(make_email_event())

    assert len(calls) == 1


def test_relate_below_floor_never_confirms(monkeypatch):
    monkeypatch.setattr(
        relating,
        "_candidates",
        lambda event: [
            {"task_gid": "111", "title": "x", "score": relating.SIMILARITY_FLOOR - 0.01}
        ],
    )
    monkeypatch.setattr(
        relating,
        "_confirm",
        lambda event, rows: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    best_score, neighbours, found = backtest._relate(make_email_event())

    assert best_score == relating.SIMILARITY_FLOOR - 0.01
    assert found.task_gid is None
    assert "111" in neighbours


def test_relate_at_or_above_floor_confirms_and_reports_the_match(monkeypatch):
    monkeypatch.setattr(
        relating, "_candidates", lambda event: [{"task_gid": "111", "title": "x", "score": 0.9}]
    )
    monkeypatch.setattr(
        relating,
        "_confirm",
        lambda event, rows: Match(task_gid="111", resolves=True, reason="same reservation"),
    )

    best_score, neighbours, found = backtest._relate(make_email_event())

    assert best_score == 0.9
    assert found.task_gid == "111"
    assert found.resolves is True


def test_relate_with_no_candidates_never_confirms(monkeypatch):
    monkeypatch.setattr(relating, "_candidates", lambda event: [])
    monkeypatch.setattr(
        relating,
        "_confirm",
        lambda event, rows: (_ for _ in ()).throw(AssertionError("must not be called")),
    )

    best_score, neighbours, found = backtest._relate(make_email_event())

    assert best_score == 0.0
    assert neighbours == ""
    assert found.task_gid is None


def test_relate_candidate_lookup_failure_degrades_to_no_match(monkeypatch):
    def boom(event):
        raise RuntimeError("vertex down")

    monkeypatch.setattr(relating, "_candidates", boom)

    best_score, neighbours, found = backtest._relate(make_email_event())

    assert best_score == 0.0
    assert "error" in neighbours
    assert found.task_gid is None


def test_relate_confirm_failure_degrades_to_no_match(monkeypatch):
    monkeypatch.setattr(
        relating, "_candidates", lambda event: [{"task_gid": "111", "title": "x", "score": 0.9}]
    )

    def boom(event, rows):
        raise RuntimeError("anthropic down")

    monkeypatch.setattr(relating, "_confirm", boom)

    _, _, found = backtest._relate(make_email_event())

    assert found.task_gid is None


def test_progress_output_goes_to_stderr_without_email_content(monkeypatch, capsys, tmp_path):
    """The progress lines (`run()`'s per-batch `print(..., file=sys.stderr)`
    calls) must stay off stdout and must never carry sender/subject/body
    text — stdout is the channel that gets pasted into the PR as the summary
    table, so any email content leaking onto it would be a real defect."""
    fake_row = (
        "msg-1",
        "graph-1",
        "secret-sender@example.com",
        "Secret Sender Name",
        "Secret Subject Line",
        "Secret body content that must never reach stderr",
        "2026-01-01T00:00:00",
        "ignore",
        "P3",
    )

    class FakeCursor:
        def execute(self, sql):
            pass

        def fetchall(self):
            return [fake_row]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def close(self):
            pass

    monkeypatch.setattr(backtest, "load_env", lambda path: {})
    monkeypatch.setattr(backtest, "connect", lambda env: FakeConn())

    import services.screening as screening

    monkeypatch.setattr(
        screening,
        "screen",
        lambda event: Screening(verdict="drop", priority="P3", reason="x", outcome="drop"),
    )

    args = argparse.Namespace(
        inbox_env="~/x",
        corpus=["negative"],
        ids=None,
        limit=0,
        no_relate=True,
        out=str(tmp_path / "out.tsv"),
    )
    backtest.run(args)

    captured = capsys.readouterr()
    assert "[negative]" in captured.err
    assert "[negative]" not in captured.out
    for secret in (
        "secret-sender@example.com",
        "Secret Sender Name",
        "Secret Subject Line",
        "Secret body content",
    ):
        assert secret not in captured.err


def test_render_comment_resolves_true_uses_close_lead():
    event = make_email_event(subject="Rental car confirmation", web_link=None)
    comment = backtest._render_comment(event, "same reservation, now confirmed", True)

    assert comment == (
        "Looks resolved — close this task if you agree. "
        "Rental car confirmation — same reservation, now confirmed"
    )


def test_render_comment_resolves_false_uses_related_lead():
    event = make_email_event(subject="Follow-up on rental car", web_link=None)
    comment = backtest._render_comment(event, "same trip, different leg", False)

    assert comment == "Related email: Follow-up on rental car — same trip, different leg"


def test_render_comment_strips_trailing_dash_when_no_web_link():
    """The harness's synthetic events always set web_link to None (see
    to_event()), so the f-string's trailing ` — {web_link or ''}` collapses
    to a bare trailing ` — ` that rstrip(" —") must remove — matching
    _suppress's own handling of a missing web_link."""
    event = make_email_event(subject="Subject", web_link=None)

    comment = backtest._render_comment(event, "reason text", False)

    assert comment == "Related email: Subject — reason text"
    assert not comment.endswith("—")
    assert not comment.endswith(" ")


def test_load_ids_ignores_blanks_and_comments(tmp_path):
    p = tmp_path / "ids.txt"
    p.write_text("id-1\n\n# a full-line comment\n  id-2  \n\n# trailing comment\n")

    assert backtest.load_ids(p) == ["id-1", "id-2"]


def test_load_ids_dedupes_preserving_first_occurrence_order(tmp_path):
    p = tmp_path / "ids.txt"
    p.write_text("id-1\nid-2\nid-1\n")

    assert backtest.load_ids(p) == ["id-1", "id-2"]


def _row(mid, category="ignore", importance="P3"):
    return (
        mid,
        f"graph-{mid}",
        "sender@example.com",
        "Sender Name",
        f"Subject {mid}",
        "body",
        "2026-01-01T00:00:00",
        category,
        importance,
    )


class _IdsFakeCursor:
    """Simulates the ids-filtered subquery: each corpus's fetchall() returns
    only the rows (from a caller-supplied per-corpus table) whose id appears
    in the IN-clause params bound on execute()."""

    def __init__(self, tables: dict[str, list[tuple]]):
        self._tables = tables
        self._next_ids: list[str] = []
        self.calls: list[tuple[str, list[str] | None]] = []

    def execute(self, sql, params=None):
        # crude corpus sniff: each CORPORA entry's own (last, i.e. non-CTE)
        # WHERE-to-ORDER-BY clause is a distinct fragment of its full query.
        for name in backtest.CORPORA:
            fragment = backtest.CORPORA[name][0].rsplit("WHERE", 1)[1].split("ORDER BY")[0]
            if fragment in sql:
                self._current = name
                break
        else:  # pragma: no cover — every corpus's fragment is present by construction
            raise AssertionError(f"could not identify corpus from sql: {sql}")
        self.calls.append((self._current, params))
        wanted = set(params or [])
        self._next_ids = [row for row in self._tables.get(self._current, []) if row[0] in wanted]

    def fetchall(self):
        return self._next_ids


class _IdsFakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def test_ids_dedupes_across_corpora_and_reports_unmatched(monkeypatch, tmp_path):
    """id-b satisfies both hard_negative and regression_reclass's WHERE clause;
    since hard_negative is searched first (per --corpus order), its row wins —
    regression_reclass's copy of id-b must not also appear. id-z matches
    neither and must be reported on stderr, not silently dropped."""
    tables = {
        "hard_negative": [_row("id-a"), _row("id-b")],
        "regression_reclass": [_row("id-b", category="urgent"), _row("id-c")],
    }
    cursor = _IdsFakeCursor(tables)
    monkeypatch.setattr(backtest, "load_env", lambda path: {})
    monkeypatch.setattr(backtest, "connect", lambda env: _IdsFakeConn(cursor))

    import services.screening as screening

    monkeypatch.setattr(
        screening,
        "screen",
        lambda event: Screening(verdict="drop", priority="P3", reason="x", outcome="drop"),
    )

    ids_file = tmp_path / "probe.ids"
    ids_file.write_text("id-a\nid-b\nid-c\nid-z\n")
    args = argparse.Namespace(
        inbox_env="~/x",
        corpus=["hard_negative", "regression_reclass"],
        ids=str(ids_file),
        limit=0,
        no_relate=True,
        out=str(tmp_path / "out.tsv"),
    )

    exit_code = backtest.run(args)

    assert exit_code == 0
    out_rows = list(csv.DictReader(open(args.out), delimiter="\t"))
    by_id = {r["message_id"]: r for r in out_rows}
    assert set(by_id) == {"id-a", "id-b", "id-c"}
    # id-b came from hard_negative (searched first), not regression_reclass.
    assert by_id["id-b"]["corpus"] == "hard_negative"
    assert by_id["id-b"]["expected"] == "non_task"
    assert by_id["id-c"]["corpus"] == "regression_reclass"
    assert by_id["id-c"]["expected"] == "task"


def test_ids_unmatched_id_reported_on_stderr(monkeypatch, tmp_path, capsys):
    tables = {"hard_negative": [_row("id-a")]}
    cursor = _IdsFakeCursor(tables)
    monkeypatch.setattr(backtest, "load_env", lambda path: {})
    monkeypatch.setattr(backtest, "connect", lambda env: _IdsFakeConn(cursor))

    import services.screening as screening

    monkeypatch.setattr(
        screening,
        "screen",
        lambda event: Screening(verdict="drop", priority="P3", reason="x", outcome="drop"),
    )

    ids_file = tmp_path / "probe.ids"
    ids_file.write_text("id-a\nid-does-not-exist\n")
    args = argparse.Namespace(
        inbox_env="~/x",
        corpus=["hard_negative"],
        ids=str(ids_file),
        limit=0,
        no_relate=True,
        out=str(tmp_path / "out.tsv"),
    )

    backtest.run(args)

    captured = capsys.readouterr()
    assert "id-does-not-exist" in captured.err
    assert "1 id(s)" in captured.err


def test_ids_empty_file_refuses_full_corpus_fallback(monkeypatch, tmp_path, capsys):
    """An --ids file that resolves to zero usable ids (e.g. only comments and
    blank lines) must not silently fall through to an unfiltered replay of
    every corpus — that is the exact 65-minute, ~$3 run --ids exists to
    avoid. The fake cursor asserts if it is ever queried at all."""

    class _BoomCursor:
        def execute(self, sql, params=None):
            raise AssertionError("must not query any corpus when --ids resolves to none")

    class _BoomConn:
        def cursor(self):
            return _BoomCursor()

        def close(self):
            pass

    monkeypatch.setattr(backtest, "load_env", lambda path: {})
    monkeypatch.setattr(backtest, "connect", lambda env: _BoomConn())

    ids_file = tmp_path / "empty.ids"
    ids_file.write_text("# just a comment\n\n   \n")
    out_path = tmp_path / "out.tsv"
    args = argparse.Namespace(
        inbox_env="~/x",
        corpus=None,
        ids=str(ids_file),
        limit=0,
        no_relate=True,
        out=str(out_path),
    )

    exit_code = backtest.run(args)

    assert exit_code != 0
    captured = capsys.readouterr()
    assert "no usable ids" in captured.err
    assert not out_path.exists()

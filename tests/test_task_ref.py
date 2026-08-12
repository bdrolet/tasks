"""Tests for scripts/task_ref.py — the 3-char base36 refs on task listings.

The interesting behaviour is `assign`: refs must be stable per GID, unique
within a listing, and independent of the order results arrived in. The
collision path (two GIDs hashing to the same ref) is rare enough in practice
that it needs a hand-picked pair to exercise at all.
"""

import importlib.util
import io
import json
import random
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "task_ref.py"

# Two real GIDs that collide: both hash to "thk" at salt 0. Found by scanning
# 1217000000000000+ — the space is 36**3, so a pair turns up quickly.
COLLIDING = ("1217000000000015", "1217000000000230")


def _load():
    spec = importlib.util.spec_from_file_location("task_ref", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


task_ref = _load()


# --- ref() ---------------------------------------------------------------


def test_ref_is_three_base36_chars():
    r = task_ref.ref("1217130164408154")
    assert len(r) == task_ref.WIDTH
    assert set(r) <= set(task_ref.ALPHABET)


def test_ref_is_deterministic():
    assert task_ref.ref("1217130164408154") == task_ref.ref("1217130164408154")


def test_salt_changes_the_ref():
    gid = "1217130164408154"
    assert task_ref.ref(gid, 1) != task_ref.ref(gid, 0)


def test_the_colliding_pair_really_collides():
    """Guards the fixture itself — if this fails the collision tests are vacuous."""
    a, b = COLLIDING
    assert task_ref.ref(a) == task_ref.ref(b)


# --- assign() ------------------------------------------------------------


def test_assign_maps_every_gid():
    gids = ["1217130164408154", "1217286466869299", "1217342697693471"]
    assert set(task_ref.assign(gids)) == set(gids)


def test_assign_is_unique_within_a_listing():
    gids = [str(1217000000000000 + i) for i in range(200)]
    refs = task_ref.assign(gids)
    assert len(set(refs.values())) == len(gids)


def test_assign_resolves_a_collision():
    a, b = COLLIDING
    refs = task_ref.assign([a, b])
    assert refs[a] != refs[b]
    # Sorted order decides: the lower GID keeps the unsalted ref.
    assert refs[a] == task_ref.ref(a)
    assert refs[b] == task_ref.ref(b, 1)


def test_assign_is_order_independent():
    """The whole point of sorting inside assign: same set in, same map out."""
    gids = [str(1217000000000000 + i) for i in range(60)] + list(COLLIDING)
    baseline = task_ref.assign(gids)
    rng = random.Random(1234)
    for _ in range(20):
        shuffled = gids[:]
        rng.shuffle(shuffled)
        assert task_ref.assign(shuffled) == baseline


def test_ref_is_stable_across_listings_without_a_collision():
    """A task keeps its ref between two unrelated searches — the reason refs are hashed."""
    gid = "1217130164408154"
    monday = task_ref.assign([gid, "1217286466869299"])
    friday = task_ref.assign([gid, "1217343933689695", "1217342697693471"])
    assert monday[gid] == friday[gid]


def test_assign_handles_an_empty_listing():
    assert task_ref.assign([]) == {}


def test_documented_collision_rate_at_25_rows():
    """The docstring quotes a rate; keep it honest.

    Theory for 25 draws from 36**3 is ~0.64%. Assert a band, not a point, so
    this fails on a real regression (a bad hash, a narrower space) rather than
    on sampling noise.
    """
    rng = random.Random(0)
    trials, collisions = 5000, 0
    for _ in range(trials):
        gids = [str(rng.randrange(10**15, 10**16)) for _ in range(25)]
        if len({task_ref.ref(g) for g in gids}) < 25:
            collisions += 1
    assert 0.003 < collisions / trials < 0.012


# --- location() ----------------------------------------------------------


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"project": "Family", "section": "Inbox"}, "Family/Inbox"),
        ({"project": "Family", "section": None}, "Family"),
        ({"project": None, "section": None}, "—"),
        # A subtask hit: parent wins over project/section, which the API nulls.
        ({"parent": "Pacifica repairs", "project": None}, "subtask of Pacifica repairs"),
        (
            {"parent": "Pacifica repairs", "project": "Family", "section": "Inbox"},
            "subtask of Pacifica repairs",
        ),
    ],
)
def test_location(result, expected):
    assert task_ref.location(result) == expected


# --- annotate mode -------------------------------------------------------


def _run_stdin(monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.argv", ["task_ref.py"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    code = task_ref.main()
    return code, capsys.readouterr()


def test_annotate_emits_one_tsv_row_per_result_in_api_order(monkeypatch, capsys):
    payload = {
        "results": [
            {
                "task_gid": "1217130164408154",
                "name": "Order Parts",
                "due_on": "2026-08-06",
                "parent": "Pacifica repairs",
            },
            {
                "task_gid": "1217342697693471",
                "name": "[P1] Schedule visit",
                "due_on": "2026-08-12",
                "project": "Family",
                "section": "Inbox",
            },
        ]
    }
    code, captured = _run_stdin(monkeypatch, capsys, payload)
    assert code == 0
    rows = [line.split("\t") for line in captured.out.strip().split("\n")]
    assert len(rows) == 2
    assert rows[0][1:] == [
        "1217130164408154",
        "2026-08-06",
        "Order Parts",
        "subtask of Pacifica repairs",
    ]
    assert rows[1][1:] == ["1217342697693471", "2026-08-12", "[P1] Schedule visit", "Family/Inbox"]
    assert rows[0][0] == task_ref.ref("1217130164408154")


def test_annotate_renders_a_missing_due_date_as_a_dash(monkeypatch, capsys):
    payload = {
        "results": [
            {"task_gid": "1217130164408154", "name": "Someday", "due_on": None, "project": "Family"}
        ]
    }
    _, captured = _run_stdin(monkeypatch, capsys, payload)
    assert captured.out.split("\t")[2] == "—"


def test_annotate_on_empty_results_is_a_clean_no_op(monkeypatch, capsys):
    code, captured = _run_stdin(monkeypatch, capsys, {"results": []})
    assert code == 0
    assert captured.out == ""


def test_annotate_rejects_non_json_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["task_ref.py"])
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert task_ref.main() == 1
    assert "not JSON" in capsys.readouterr().err


def test_annotate_rejects_a_payload_that_is_not_a_search_response(monkeypatch, capsys):
    code, captured = _run_stdin(monkeypatch, capsys, {"task_gid": "1217130164408154"})
    assert code == 1
    assert "no 'results' or 'subtasks' key" in captured.err


def test_annotate_refs_the_subtasks_of_a_fetched_task(monkeypatch, capsys):
    """A /tasks/<gid> fetch has no `results` — its subtasks are the listing."""
    payload = {
        "name": "Pacifica repairs",
        "subtasks": [
            {
                "task_gid": "1217130164408154",
                "name": "Order Parts",
                "completed": False,
                "due_on": "2026-08-06",
            },
            {
                "task_gid": "1217286466869299",
                "name": "Book the shop",
                "completed": True,
                "due_on": None,
            },
        ],
    }
    code, captured = _run_stdin(monkeypatch, capsys, payload)
    assert code == 0
    rows = [line.split("\t") for line in captured.out.strip().split("\n")]
    assert [r[1] for r in rows] == ["1217130164408154", "1217286466869299"]
    # Subtasks belong to their parent, so the API sends no project/section.
    assert rows[0][4] == "—"


def test_a_subtask_keeps_its_ref_between_a_search_and_a_parent_fetch(monkeypatch, capsys):
    """The same subtask surfaces both ways; the handle must not change."""
    gid = "1217130164408154"
    from_search = task_ref.assign([gid])[gid]
    from_fetch = task_ref.assign([gid, "1217286466869299"])[gid]
    assert from_search == from_fetch == task_ref.ref(gid)


def test_results_wins_when_a_payload_somehow_has_both(monkeypatch, capsys):
    payload = {
        "results": [{"task_gid": "1217342697693471", "name": "From results"}],
        "subtasks": [{"task_gid": "1217130164408154", "name": "From subtasks"}],
    }
    _, captured = _run_stdin(monkeypatch, capsys, payload)
    assert captured.out.strip().split("\t")[3] == "From results"


# --- argv mode -----------------------------------------------------------


def test_argv_mode_prints_ref_and_gid_in_the_order_given(monkeypatch, capsys):
    a, b = "1217342697693471", "1217130164408154"
    monkeypatch.setattr("sys.argv", ["task_ref.py", a, b])
    assert task_ref.main() == 0
    rows = [line.split("\t") for line in capsys.readouterr().out.strip().split("\n")]
    assert [r[1] for r in rows] == [a, b]
    assert rows[0][0] == task_ref.ref(a)


def test_argv_mode_matches_annotate_mode(monkeypatch, capsys):
    """Both entry points must agree, or a ref quoted from one won't resolve in the other."""
    gids = list(COLLIDING)
    monkeypatch.setattr("sys.argv", ["task_ref.py", *gids])
    task_ref.main()
    argv_refs = {
        r.split("\t")[1]: r.split("\t")[0] for r in capsys.readouterr().out.strip().split("\n")
    }
    assert argv_refs == task_ref.assign(gids)

"""Tests for scripts/task_sessions.py — one agent-deck Claude session per task.

The script's I/O ends (the tasks-api calls and every `deck-sessions` call)
are seams the tests pass fakes into. What's pinned down here is the task
half: the context block, the title, the spec each task becomes, and how
deck-sessions' results map back to the rows task-sessions has always printed.
The agent-deck behaviour itself is tested in ~/src/scripts/deck-sessions/tests.
"""

import importlib.util
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "task_sessions.py"


def _load():
    spec = importlib.util.spec_from_file_location("task_sessions", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ts = _load()


TASK = {
    "task_gid": "1218118170820306",
    "name": "[P1] Investigate Google Workspace subscription before Oct bill",
    "notes": "An untracked $64.70/mo charge on Wells Fargo checking.",
    "completed": False,
    "due_on": "2026-09-15",
    "project": "Ben's Board",
    "section": "Inbox",
    "tags": ["finances"],
    "assignee": "Ben Drolet",
    "parent": None,
    "permalink_url": "https://app.asana.com/1/1206/project/1207/task/1218118170820306",
    "subtasks": [
        {
            "task_gid": "1218558398903885",
            "name": "Decide whether to keep benjamin@ seat",
            "completed": False,
            "due_on": None,
            "permalink_url": "https://app.asana.com/1/1206/task/1218558398903885",
        }
    ],
    "comments": [
        {
            "gid": "77",
            "text": "Charge migrated onto Visa 2672 in June.",
            "created_by": "Ben Drolet",
            "created_at": "2026-09-16T18:02:00Z",
            "is_editable": True,
        }
    ],
}


# --- context_block -------------------------------------------------------


def test_context_block_carries_the_identifying_fields():
    block = ts.context_block(TASK, ref="gws")

    assert "1218118170820306" in block
    assert "[P1] Investigate Google Workspace subscription before Oct bill" in block
    assert TASK["permalink_url"] in block
    assert "2026-09-15" in block
    assert "Ben's Board" in block
    assert "Inbox" in block
    assert "finances" in block


def test_context_block_includes_notes_and_comments():
    block = ts.context_block(TASK, ref="gws")

    assert "An untracked $64.70/mo charge" in block
    assert "Charge migrated onto Visa 2672 in June." in block
    assert "Ben Drolet" in block
    assert "2026-09-16" in block


def test_context_block_lists_subtasks_with_their_own_refs():
    block = ts.context_block(TASK, ref="gws")

    assert "1218558398903885" in block
    assert "Decide whether to keep benjamin@ seat" in block
    # A subtask's ref is the same hash a listing would give it.
    assert ts.ref_for("1218558398903885") in block


def test_context_block_survives_a_task_with_nothing_but_a_name():
    bare = {"task_gid": "1217000000000001", "name": "Call the dentist"}

    block = ts.context_block(bare, ref="dnt")

    assert "Call the dentist" in block
    assert "1217000000000001" in block
    assert "None" not in block


def test_context_block_says_a_subtask_belongs_to_its_parent():
    child = dict(TASK, parent={"gid": "1217999", "name": "Q4 cost review"})

    block = ts.context_block(child, ref="gws")

    assert "Q4 cost review" in block
    assert "1217999" in block


# --- session_name --------------------------------------------------------


def test_session_name_leads_with_the_ref():
    assert ts.session_name("gws", "Pay the water bill").startswith("gws ")


def test_session_name_truncates_a_long_title():
    name = ts.session_name("gws", "x" * 200)

    assert len(name) <= ts.NAME_MAX
    assert name.endswith("…")


# --- a fake deck-sessions ------------------------------------------------


class FakeDeckSessions:
    """Records each deck-sessions call (argv + stdin) and answers with canned results.

    `results` maps a GID to the result `ensure` returns for it; any GID not
    listed comes back `launched` (or `would launch` without --apply).
    """

    def __init__(self, results=None, sessions=None, exit_code=0, stderr=""):
        self.results = results or {}
        self.listed = sessions or []
        self.exit_code = exit_code
        self.stderr = stderr
        self.calls = []

    def specs(self):
        argv, stdin = next(c for c in self.calls if c[0][0] == "ensure")
        return [json.loads(line) for line in stdin.splitlines()]

    def __call__(self, argv, stdin=None):
        self.calls.append((argv, stdin))
        if self.exit_code:
            return subprocess.CompletedProcess(argv, self.exit_code, "", self.stderr)
        if argv[0] == "ensure":
            apply = "--apply" in argv
            out = []
            for line in stdin.splitlines():
                key = json.loads(line)["key"]
                default = (
                    {
                        "outcome": "launched",
                        "deck_id": "abcdef12-1",
                        "status": "starting",
                        "group": "tasks",
                    }
                    if apply
                    else {"outcome": "would launch"}
                )
                out.append(
                    json.dumps(
                        {
                            "key": key,
                            "detail": None,
                            "deck_id": None,
                            "status": None,
                            "group": None,
                            "adopted": False,
                        }
                        | self.results.get(key, default)
                    )
                )
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", self.stderr)
        if argv[0] == "sessions":
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.listed), "")
        if argv[0] == "archive":
            apply = "--apply" in argv
            ids = [a for a in argv[1:] if a != "--apply"]
            out = []
            for sid in ids:
                status = next(s["status"] for s in self.listed if s["deck_id"] == sid)
                outcome = (
                    "kept (busy)"
                    if status == "running"
                    else "archived"
                    if apply
                    else "would archive"
                )
                out.append(json.dumps({"deck_id": sid, "outcome": outcome, "detail": None}))
            return subprocess.CompletedProcess(argv, 0, "\n".join(out) + "\n", "")
        raise AssertionError(argv)


GID = TASK["task_gid"]
REF = ts.ref_for(GID)


def _main(deck, argv, monkeypatch, tmp_path, env=None, task=TASK, fetch=None):
    monkeypatch.setattr(ts, "CONTEXT_DIR", tmp_path / "ctx")
    return ts.main(
        argv,
        deck=deck,
        fetch=fetch or (lambda gid: dict(task, task_gid=gid)),
        search=lambda **_: [],
        env=env or {},
    )


# --- the spec each task becomes ------------------------------------------


def test_one_spec_per_task_with_the_task_fields(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    (spec,) = deck.specs()
    assert spec == {
        "key_hint": "asana_gid",
        "key": GID,
        "title": ts.session_name(REF, TASK["name"]),
        "adopt_prefix": f"{REF} ",
        "context": ts.context_block(dict(TASK), REF),
        "context_path": str(tmp_path / "ctx" / f"{GID}.md"),
        "cwd": str(ts.REPO),
        "group": "tasks",
        "parent": "conductor-tasks",
        "parent_is_default": True,
        "hints": {"permalink": TASK["permalink_url"]},
        "model": None,
        "idle_timeout": None,
        "prompt": ts.START_PROMPT,
        "reopen_prompt": None,
        "refresh": False,
    }


def test_context_path_defaults_to_the_task_context_dir():
    # The 13 live sessions have this path baked into their stored extra args.
    assert Path.home() / ".claude" / "task-context" == ts.CONTEXT_DIR


def test_idle_sends_no_prompt(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, "--spawn", "--idle"], monkeypatch, tmp_path)

    assert deck.specs()[0]["prompt"] is None


def test_prompt_overrides_the_start_prompt(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, "--spawn", "--prompt", "prep this"], monkeypatch, tmp_path)

    assert deck.specs()[0]["prompt"] == "prep this"


def test_prompt_file_is_read(monkeypatch, tmp_path):
    deck = FakeDeckSessions()
    (tmp_path / "p.txt").write_text("from a file")

    _main(deck, [GID, "--prompt-file", str(tmp_path / "p.txt")], monkeypatch, tmp_path)

    assert deck.specs()[0]["prompt"] == "from a file"


def test_idle_and_prompt_conflict(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(FakeDeckSessions(), [GID, "--idle", "--prompt", "x"], monkeypatch, tmp_path)


def test_model_idle_timeout_and_refresh_pass_through(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(
        deck, [GID, "--model", "opus", "--idle-timeout", "24h", "--refresh"], monkeypatch, tmp_path
    )

    spec = deck.specs()[0]
    assert (spec["model"], spec["idle_timeout"], spec["refresh"]) == ("opus", "24h", True)


def test_a_task_without_a_permalink_sends_no_hint(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID], monkeypatch, tmp_path, task=dict(TASK, permalink_url=None))

    assert deck.specs()[0]["hints"] == {}


def test_apply_is_passed_only_with_spawn(monkeypatch, tmp_path):
    deck = FakeDeckSessions()
    _main(deck, [GID], monkeypatch, tmp_path)
    assert deck.calls[0][0] == ["ensure"]

    deck = FakeDeckSessions()
    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)
    assert deck.calls[0][0] == ["ensure", "--apply"]


def test_repeated_gids_become_one_spec(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, GID, "--spawn"], monkeypatch, tmp_path)

    assert len(deck.specs()) == 1


def test_explicit_gids_are_not_capped_by_limit(monkeypatch, tmp_path):
    deck = FakeDeckSessions()
    gids = [f"12170000000000{i:02d}" for i in range(3)]

    _main(deck, gids + ["--spawn", "--limit", "1"], monkeypatch, tmp_path)

    assert len(deck.specs()) == 3


def test_completed_and_unfetchable_tasks_are_never_sent(monkeypatch, tmp_path, capsys):
    deck = FakeDeckSessions()
    done = "1217000000000002"

    def fetch(gid):
        if gid == "1217000000000003":
            raise OSError("boom")
        return dict(TASK, task_gid=gid, completed=gid == done)

    _main(deck, [GID, done, "1217000000000003", "--spawn"], monkeypatch, tmp_path, fetch=fetch)

    assert [s["key"] for s in deck.specs()] == [GID]
    out = capsys.readouterr().out
    assert "skipped (completed)" in out and "fetch failed: boom" in out


def test_nothing_to_send_never_calls_deck_sessions(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path, task=dict(TASK, completed=True))

    assert deck.calls == []


# --- group and parent configuration --------------------------------------


def test_group_flag_beats_env_beats_default(monkeypatch, tmp_path):
    deck = FakeDeckSessions()
    _main(deck, [GID], monkeypatch, tmp_path, env={"TASK_SESSIONS_GROUP": "work"})
    assert deck.specs()[0]["group"] == "work"

    deck = FakeDeckSessions()
    _main(
        deck, [GID, "--group", "asana"], monkeypatch, tmp_path, env={"TASK_SESSIONS_GROUP": "work"}
    )
    assert deck.specs()[0]["group"] == "asana"


def test_parent_env_is_explicit(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID], monkeypatch, tmp_path, env={"TASK_SESSIONS_PARENT": "boss"})

    spec = deck.specs()[0]
    assert (spec["parent"], spec["parent_is_default"]) == ("boss", False)


def test_parent_none_opts_out(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, "--parent", "none"], monkeypatch, tmp_path)

    assert deck.specs()[0]["parent"] == "none"


def test_parent_self_uses_the_calling_session(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(
        deck,
        [GID, "--parent", "self"],
        monkeypatch,
        tmp_path,
        env={"AGENTDECK_INSTANCE_ID": "781a5faa-1"},
    )

    assert deck.specs()[0]["parent"] == "781a5faa-1"


def test_parent_self_outside_agent_deck_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(FakeDeckSessions(), [GID, "--parent", "self"], monkeypatch, tmp_path)


def test_inherit_group_sends_a_null_group(monkeypatch, tmp_path):
    deck = FakeDeckSessions()

    _main(deck, [GID, "--parent", "conductor", "--inherit-group"], monkeypatch, tmp_path)

    assert deck.specs()[0]["group"] is None


def test_inherit_group_without_a_parent_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(
            FakeDeckSessions(), [GID, "--parent", "none", "--inherit-group"], monkeypatch, tmp_path
        )


def test_a_missing_explicit_parent_is_an_error(monkeypatch, tmp_path):
    deck = FakeDeckSessions(
        {GID: {"outcome": "failed", "detail": "parent nobody not found in agent-deck"}}
    )

    with pytest.raises(SystemExit, match="parent nobody not found"):
        _main(deck, [GID, "--spawn", "--parent", "nobody"], monkeypatch, tmp_path)


def test_deck_sessions_warnings_are_forwarded(monkeypatch, tmp_path, capsys):
    deck = FakeDeckSessions(
        stderr="warning: parent conductor-tasks not found; launching without a parent\n"
    )

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "conductor-tasks not found" in capsys.readouterr().err


# --- outcome mapping -----------------------------------------------------

OUTCOMES = [
    ({"outcome": "launched", "deck_id": "abcdef12-1", "status": "starting"}, "launched abcdef12"),
    (
        {"outcome": "already open", "deck_id": "11223344-9", "status": "waiting"},
        "already open 11223344 (waiting)",
    ),
    (
        {"outcome": "already open", "deck_id": "11223344-9", "status": "idle", "adopted": True},
        "already open 11223344 (idle) · adopted",
    ),
    (
        {"outcome": "already open", "detail": "session exists: 99"},
        "already open (session exists: 99)",
    ),
    ({"outcome": "reopened", "deck_id": "11223344-9", "status": "stopped"}, "reopened 11223344"),
    ({"outcome": "refreshed", "deck_id": "11223344-9", "status": "idle"}, "refreshed 11223344"),
    ({"outcome": "duplicate", "detail": "11223344-9,55667788-9"}, "duplicate 11223344,55667788"),
    ({"outcome": "failed", "detail": "tmux exploded"}, "failed: tmux exploded"),
    (
        {"outcome": "failed", "deck_id": "11223344-9", "detail": "refresh failed: busy"},
        "refresh failed: busy",
    ),
    ({"outcome": "would launch"}, "would launch"),
    ({"outcome": "would reopen", "deck_id": "11223344-9"}, "would reopen 11223344"),
    ({"outcome": "would refresh", "deck_id": "11223344-9"}, "would refresh 11223344"),
]


@pytest.mark.parametrize(("result", "text"), OUTCOMES)
def test_outcomes_map_to_the_familiar_strings(result, text, monkeypatch, tmp_path, capsys):
    deck = FakeDeckSessions({GID: result})

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert capsys.readouterr().out.rstrip().endswith(f"→ {text}")


def test_tsv_row_shape(monkeypatch, tmp_path, capsys):
    _main(FakeDeckSessions(), [GID, "--spawn"], monkeypatch, tmp_path)

    (line,) = capsys.readouterr().out.splitlines()
    assert line.split("\t") == [
        REF,
        GID,
        "2026-09-15",
        "Ben's Board/Inbox",
        TASK["name"],
        TASK["permalink_url"],
        "→ launched abcdef12",
    ]


def test_json_rows_carry_what_the_agent_reports(monkeypatch, tmp_path, capsys):
    _main(FakeDeckSessions(), [GID, "--spawn", "--json"], monkeypatch, tmp_path)

    (row,) = json.loads(capsys.readouterr().out)
    assert set(row) == {
        "ref",
        "gid",
        "name",
        "due",
        "project",
        "section",
        "permalink",
        "outcome",
        "deck_id",
        "status",
        "group",
        "attach",
    }
    assert row["permalink"] == TASK["permalink_url"]
    assert row["project"] == "Ben's Board" and row["section"] == "Inbox"
    assert row["outcome"] == "launched abcdef12"
    assert row["deck_id"] == "abcdef12-1" and row["group"] == "tasks"
    assert row["attach"] == "agent-deck session attach abcdef12"


# --- failures ------------------------------------------------------------


def test_registry_unavailable_exits_2(monkeypatch, tmp_path, capsys):
    deck = FakeDeckSessions(exit_code=2, stderr='{"error": "registry unavailable: tmux down"}\n')

    assert _main(deck, [GID, "--spawn"], monkeypatch, tmp_path) == 2
    err = capsys.readouterr().err
    assert "registry unavailable: tmux down — nothing launched" in err


def test_a_missing_deck_sessions_binary_exits_2(monkeypatch, tmp_path, capsys):
    def missing(argv, stdin=None):
        raise FileNotFoundError("deck-sessions")

    assert _main(missing, [GID, "--spawn"], monkeypatch, tmp_path) == 2
    assert "deck-sessions not on PATH" in capsys.readouterr().err


# --- prune ---------------------------------------------------------------


def _listed(sid, gid, status="idle", archived=False):
    return {
        "deck_id": sid,
        "key": gid,
        "title": f"t {gid}",
        "group": "tasks",
        "status": status,
        "archived": archived,
    }


def test_prune_archives_only_the_completed_tasks_session(monkeypatch, tmp_path, capsys):
    open_gid = "1217000000000009"
    deck = FakeDeckSessions(
        sessions=[
            _listed("11223344-9", GID),
            _listed("99999999-1", open_gid),
            _listed("77777777-1", "1217000000000008", archived=True),
        ]
    )
    fetch = lambda gid: {"task_gid": gid, "completed": gid == GID}  # noqa: E731

    _main(deck, ["--prune", "--spawn"], monkeypatch, tmp_path, fetch=fetch)

    assert deck.calls[0][0] == ["sessions", "--key-hint", "asana_gid", "--group", "tasks"]
    assert deck.calls[1][0] == ["archive", "--apply", "11223344-9"]
    assert capsys.readouterr().out == f"{GID}\tt {GID}\t→ pruned 11223344\n"


def test_prune_treats_a_deleted_task_as_done(monkeypatch, tmp_path):
    deck = FakeDeckSessions(sessions=[_listed("11223344-9", GID)])

    def fetch(gid):
        raise urllib.error.HTTPError("u", 404, "gone", {}, None)

    _main(deck, ["--prune"], monkeypatch, tmp_path, fetch=fetch)

    assert deck.calls[1][0] == ["archive", "11223344-9"]


def test_prune_dry_run_and_busy(monkeypatch, tmp_path, capsys):
    deck = FakeDeckSessions(
        sessions=[_listed("11223344-9", GID), _listed("55667788-9", "1217000000000002", "running")]
    )

    _main(deck, ["--prune"], monkeypatch, tmp_path, task=dict(TASK, completed=True))

    out = capsys.readouterr().out
    assert "→ would prune 11223344" in out and "→ kept (busy) 55667788" in out


def test_prune_with_nothing_done_archives_nothing(monkeypatch, tmp_path):
    deck = FakeDeckSessions(sessions=[_listed("11223344-9", GID)])

    _main(deck, ["--prune", "--spawn"], monkeypatch, tmp_path)

    assert [c[0][0] for c in deck.calls] == ["sessions"]

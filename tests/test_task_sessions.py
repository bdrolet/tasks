"""Tests for scripts/task_sessions.py — one agent-deck Claude session per task.

The script's I/O ends (the tasks-api calls and every `agent-deck` command)
are seams the tests pass fakes into. What's worth pinning down is everything
in between: the context block a session boots with, the launch argv, and the
lookup that decides whether a task already has a session, needs one reopened,
or needs a new one — with agent-deck's registry as the only state.
"""

import importlib.util
import json
import subprocess
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


# --- a fake agent-deck ---------------------------------------------------


class FakeDeck:
    """Answers the agent-deck commands the script runs, and records each argv."""

    def __init__(self, sessions=None, hints=None, list_fails=False, launch_code=None):
        self.sessions = sessions or []
        self.hints = hints or {}
        self.list_fails = list_fails
        self.launch_code = launch_code
        self.calls = []
        self.stdin = []
        self.groups = {}

    def _ok(self, argv, body, code=0):
        return subprocess.CompletedProcess(argv, code, stdout=json.dumps(body), stderr="")

    def __call__(self, argv, stdin=None):
        self.calls.append(argv)
        self.stdin.append(stdin)
        verb = argv[1:3]
        if argv[1] == "list":
            if self.list_fails:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="tmux down")
            return self._ok(argv, self.sessions)
        if argv[1] == "launch":
            if self.launch_code:
                return self._ok(
                    argv, {"success": False, "code": self.launch_code, "error": "nope"}, 1
                )
            title = argv[argv.index("-t") + 1]
            return self._ok(
                argv,
                {
                    "success": True,
                    "id": "abcdef12-1",
                    "status": "starting",
                    "group": "tasks",
                    "title": title,
                },
            )
        if verb == ["session", "annotate"]:
            return self._ok(argv, {"success": True, "hints": self.hints.get(argv[3], {})})
        if verb == ["group", "show"]:
            if argv[3] in self.groups:
                return self._ok(argv, {"success": True, "max_concurrent": self.groups[argv[3]]})
            return self._ok(argv, {"success": False, "code": "NOT_FOUND"}, 2)
        return self._ok(argv, {"success": True})

    def verbs(self):
        return [" ".join(a[1:3]) for a in self.calls]


GID = TASK["task_gid"]
CONDUCTOR = {
    "id": "14ba1caf-1",
    "title": "conductor-tasks",
    "group": "conductor",
    "status": "waiting",
}
REF = ts.ref_for(GID)


def _main(deck, argv, monkeypatch, tmp_path, env=None, task=TASK):
    monkeypatch.setattr(ts, "CONTEXT_DIR", tmp_path / "ctx")
    return ts.main(
        argv,
        runner=deck,
        fetch=lambda gid: dict(task, task_gid=gid),
        search=lambda **_: [],
        env=env or {},
    )


def _launch(deck):
    return next(a for a in deck.calls if a[1] == "launch")


# --- launch --------------------------------------------------------------


def test_launch_defaults_to_the_tasks_conductor_and_group(monkeypatch, tmp_path):
    deck = FakeDeck([CONDUCTOR])

    assert _main(deck, [GID, "--spawn"], monkeypatch, tmp_path) == 0

    argv = _launch(deck)
    assert argv[argv.index("--parent") + 1] == "conductor-tasks"
    assert argv[argv.index("-g") + 1] == "tasks"
    assert "--no-identity" in argv
    assert f"asana_gid={GID}" in argv
    assert argv[argv.index("-t") + 1].startswith(f"{REF} ")


def test_launch_sends_the_start_prompt_by_default(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    argv = _launch(deck)
    assert argv[argv.index("--message-file") + 1] == "-"
    assert "--no-assert-done" in argv
    assert ts.START_PROMPT in deck.stdin


def test_idle_launches_with_no_message(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn", "--idle"], monkeypatch, tmp_path)

    assert "--message-file" not in _launch(deck)
    assert not any(deck.stdin)


def test_idle_and_prompt_conflict(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(FakeDeck(), [GID, "--idle", "--prompt", "x"], monkeypatch, tmp_path)


def test_a_reopened_session_is_not_prompted(monkeypatch, tmp_path):
    deck = FakeDeck([_session("stopped")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert not any(deck.stdin)


def test_launch_passes_the_context_as_a_file(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn", "--idle"], monkeypatch, tmp_path)

    argv = _launch(deck)
    assert "--extra-arg=--append-system-prompt-file" in argv
    path = tmp_path / "ctx" / f"{GID}.md"
    assert f"--extra-arg={path}" in argv
    assert (path.stat().st_mode & 0o777) == 0o600
    text = path.read_text()
    assert "## agent-deck" in text
    assert "AGENTDECK_DONE" not in text  # no parent, no sentinel rule


def test_a_prompt_goes_on_stdin_without_the_done_instruction(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn", "--prompt", "prep this"], monkeypatch, tmp_path)

    argv = _launch(deck)
    assert argv[argv.index("--message-file") + 1] == "-"
    assert "--no-assert-done" in argv
    assert "prep this" in deck.stdin


def test_dry_run_launches_nothing(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID], monkeypatch, tmp_path)

    assert "launch" not in [a[1] for a in deck.calls]
    assert "group create" not in deck.verbs()


def test_spawn_creates_a_missing_group_uncapped(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    create = next(a for a in deck.calls if a[1:3] == ["group", "create"])
    assert create[3] == "tasks"
    # create ignores --max-concurrent 0, so the cap is lifted with an update.
    update = next(a for a in deck.calls if a[1:3] == ["group", "update"])
    assert update[3] == "tasks"
    assert update[update.index("--max-concurrent") + 1] == "0"


def test_an_existing_groups_cap_is_left_alone(monkeypatch, tmp_path, capsys):
    deck = FakeDeck()
    deck.groups["tasks"] = 1

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "group update" not in deck.verbs()
    assert "max_concurrent=1" in capsys.readouterr().err


# --- group and parent configuration --------------------------------------


def test_group_flag_beats_env_beats_default(monkeypatch, tmp_path):
    deck = FakeDeck()
    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path, env={"TASK_SESSIONS_GROUP": "work"})
    assert _launch(deck)[_launch(deck).index("-g") + 1] == "work"

    deck = FakeDeck()
    _main(
        deck,
        [GID, "--spawn", "--group", "asana"],
        monkeypatch,
        tmp_path,
        env={"TASK_SESSIONS_GROUP": "work"},
    )
    assert _launch(deck)[_launch(deck).index("-g") + 1] == "asana"


def test_parent_self_uses_the_calling_session(monkeypatch, tmp_path):
    deck = FakeDeck([{"id": "781a5faa-1", "title": "me", "group": "my-sessions"}])

    _main(
        deck,
        [GID, "--spawn", "--parent", "self"],
        monkeypatch,
        tmp_path,
        env={"AGENTDECK_INSTANCE_ID": "781a5faa-1"},
    )

    argv = _launch(deck)
    assert argv[argv.index("--parent") + 1] == "781a5faa-1"
    assert "--no-parent" not in argv
    assert "AGENTDECK_DONE" in (tmp_path / "ctx" / f"{GID}.md").read_text()


def test_parent_self_outside_agent_deck_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(FakeDeck(), [GID, "--parent", "self"], monkeypatch, tmp_path)


def test_inherit_group_drops_the_group_flag(monkeypatch, tmp_path):
    deck = FakeDeck([{"id": "c0ffee00-1", "title": "conductor", "group": "conductor"}])

    _main(deck, [GID, "--spawn", "--parent", "conductor", "--inherit-group"], monkeypatch, tmp_path)

    argv = _launch(deck)
    assert "-g" not in argv
    assert "--inherit-group" in argv


def test_inherit_group_without_a_parent_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(FakeDeck(), [GID, "--inherit-group"], monkeypatch, tmp_path)


# --- lookup and the state machine ----------------------------------------


def _session(status, sid="11223344-9", title=None, group="tasks", **extra):
    return {"id": sid, "title": title or f"{REF} x", "group": group, "status": status, **extra}


def test_a_live_session_is_left_alone(monkeypatch, tmp_path, capsys):
    deck = FakeDeck([_session("waiting")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "launch" not in [a[1] for a in deck.calls]
    assert "already open 11223344 (waiting)" in capsys.readouterr().out


def test_a_stopped_session_is_reopened_not_duplicated(monkeypatch, tmp_path, capsys):
    deck = FakeDeck([_session("stopped")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "session start" in deck.verbs()
    assert "launch" not in [a[1] for a in deck.calls]
    assert "reopened 11223344" in capsys.readouterr().out


def test_a_session_found_in_another_group_still_counts(monkeypatch, tmp_path):
    deck = FakeDeck([_session("idle", group="my-sessions")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "launch" not in [a[1] for a in deck.calls]


def test_a_hand_made_session_with_the_ref_is_adopted(monkeypatch, tmp_path):
    deck = FakeDeck([_session("idle", group="my-sessions")], {})

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    annotate = [a for a in deck.calls if a[1:3] == ["session", "annotate"] and "--set-hint" in a]
    assert annotate and annotate[0][-1] == f"asana_gid={GID}"
    assert "launch" not in [a[1] for a in deck.calls]


def test_two_sessions_for_one_task_is_refused(monkeypatch, tmp_path, capsys):
    deck = FakeDeck(
        [_session("idle"), _session("idle", sid="55667788-9")],
        {"11223344-9": {"asana_gid": GID}, "55667788-9": {"asana_gid": GID}},
    )

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "duplicate 11223344,55667788" in capsys.readouterr().out
    assert "launch" not in [a[1] for a in deck.calls]


def test_an_unreadable_registry_launches_nothing(monkeypatch, tmp_path):
    deck = FakeDeck(list_fails=True)

    assert _main(deck, [GID, "--spawn"], monkeypatch, tmp_path) == 2
    assert "launch" not in [a[1] for a in deck.calls]


def test_already_exists_from_agent_deck_is_not_a_failure(monkeypatch, tmp_path, capsys):
    deck = FakeDeck(launch_code="ALREADY_EXISTS")

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "already open" in capsys.readouterr().out


def test_repeated_gids_launch_once(monkeypatch, tmp_path):
    deck = FakeDeck()

    _main(deck, [GID, GID, "--spawn"], monkeypatch, tmp_path)

    assert [a[1] for a in deck.calls].count("launch") == 1


def test_explicit_gids_are_not_capped_by_limit(monkeypatch, tmp_path):
    deck = FakeDeck()
    gids = [f"12170000000000{i:02d}" for i in range(3)]

    _main(deck, gids + ["--spawn", "--limit", "1"], monkeypatch, tmp_path)

    assert [a[1] for a in deck.calls].count("launch") == 3


def test_refresh_sends_context_to_an_idle_session(monkeypatch, tmp_path):
    deck = FakeDeck([_session("idle")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, [GID, "--spawn", "--refresh"], monkeypatch, tmp_path)

    send = next(a for a in deck.calls if a[1:3] == ["session", "send"])
    assert send[-1] == "11223344-9"
    assert any(s and "Updated task context" in s for s in deck.stdin)


def test_refresh_never_interrupts_a_running_session(monkeypatch, tmp_path):
    deck = FakeDeck([_session("running")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, [GID, "--spawn", "--refresh"], monkeypatch, tmp_path)

    assert "session send" not in deck.verbs()


def test_prune_archives_a_completed_tasks_session(monkeypatch, tmp_path, capsys):
    deck = FakeDeck([_session("idle")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, ["--prune", "--spawn"], monkeypatch, tmp_path, task=dict(TASK, completed=True))

    assert "session archive" in deck.verbs()
    assert "pruned 11223344" in capsys.readouterr().out


def test_prune_keeps_a_running_session(monkeypatch, tmp_path, capsys):
    deck = FakeDeck([_session("running")], {"11223344-9": {"asana_gid": GID}})

    _main(deck, ["--prune", "--spawn"], monkeypatch, tmp_path, task=dict(TASK, completed=True))

    assert "session archive" not in deck.verbs()
    assert "kept (busy)" in capsys.readouterr().out


def test_json_rows_carry_what_the_agent_reports(monkeypatch, tmp_path, capsys):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn", "--json"], monkeypatch, tmp_path)

    (row,) = json.loads(capsys.readouterr().out)
    assert row["permalink"] == TASK["permalink_url"]
    assert row["project"] == "Ben's Board" and row["section"] == "Inbox"
    assert row["attach"] == "agent-deck session attach abcdef12"


def test_a_missing_default_conductor_launches_unparented(monkeypatch, tmp_path, capsys):
    deck = FakeDeck()

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "--no-parent" in _launch(deck)
    assert "conductor-tasks not found" in capsys.readouterr().err
    assert "AGENTDECK_DONE" not in (tmp_path / "ctx" / f"{GID}.md").read_text()


def test_a_missing_explicit_parent_is_an_error(monkeypatch, tmp_path):
    with pytest.raises(SystemExit):
        _main(FakeDeck(), [GID, "--spawn", "--parent", "nobody"], monkeypatch, tmp_path)


def test_parent_none_opts_out_of_the_conductor(monkeypatch, tmp_path):
    deck = FakeDeck([CONDUCTOR])

    _main(deck, [GID, "--spawn", "--parent", "none"], monkeypatch, tmp_path)

    assert "--no-parent" in _launch(deck)


def test_a_conductor_parented_session_gets_the_sentinel_rule(monkeypatch, tmp_path):
    deck = FakeDeck([CONDUCTOR])

    _main(deck, [GID, "--spawn"], monkeypatch, tmp_path)

    assert "AGENTDECK_DONE" in (tmp_path / "ctx" / f"{GID}.md").read_text()
    assert "--no-assert-done" not in _launch(deck)

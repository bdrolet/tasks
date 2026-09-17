"""Tests for scripts/task_sessions.py — one background Claude session per task.

The script's I/O ends (the tasks-api calls, `claude --bg`, `claude agents`)
are seams the tests pass fakes into. What's worth pinning down is everything
in between: the context block a spawned session boots with, the id parsed back
out of `claude --bg`, and the reconcile step that decides whether a task
already has a live session or needs a new one.
"""

import importlib.util
import json
import subprocess
from pathlib import Path

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


# --- spawn_argv ----------------------------------------------------------


def test_spawn_argv_backgrounds_a_named_session_carrying_the_context():
    argv = ts.spawn_argv(name="gws Investigate Google Workspace", context="TASK CONTEXT: …")

    assert argv[0] == "claude"
    assert "--bg" in argv
    assert argv[argv.index("-n") + 1] == "gws Investigate Google Workspace"
    assert argv[argv.index("--append-system-prompt") + 1] == "TASK CONTEXT: …"


def test_spawn_argv_never_supplies_a_session_id():
    argv = ts.spawn_argv(name="gws x", context="ctx")

    # `claude --bg --session-id <uuid>` warns "--bg manages the session id;
    # ignoring --session-id" and assigns its own. Passing one would make the
    # script confidently record an id that does not exist.
    assert "--session-id" not in argv


def test_spawn_argv_passes_no_prompt_so_the_session_idles():
    argv = ts.spawn_argv(name="gws x", context="ctx")

    # A trailing bare argument would be a prompt, and the session would start
    # a turn — and spend tokens — before anyone attached to it.
    assert argv[-1] == "ctx"


# --- parse_session_id ----------------------------------------------------


def test_parse_session_id_reads_the_id_claude_bg_prints():
    output = (
        "backgrounded · ed1b7ea6 · gws Investigate Google Workspace\n"
        "  claude agents             list sessions\n"
        "  claude attach ed1b7ea6    open in this terminal\n"
    )

    assert ts.parse_session_id(output) == "ed1b7ea6"


def test_parse_session_id_handles_the_idle_suffix_bg_adds():
    output = "backgrounded · 9cf12fa1 · probe (idle — send a prompt to start)\n"

    assert ts.parse_session_id(output) == "9cf12fa1"


def test_parse_session_id_returns_none_when_the_spawn_said_something_else():
    assert ts.parse_session_id("error: could not start session\n") is None


# --- spawn ---------------------------------------------------------------


def test_spawn_reports_the_id_claude_chose():
    def runner(argv):
        return subprocess.CompletedProcess(argv, 0, stdout="backgrounded · 07bb8d09 · gws x\n")

    assert ts.spawn("gws x", "ctx", runner=runner) == "07bb8d09"


def test_spawn_returns_none_when_claude_exits_nonzero():
    def runner(argv):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    assert ts.spawn("gws x", "ctx", runner=runner) is None


# --- reconcile -----------------------------------------------------------


def test_reconcile_keeps_gids_whose_session_is_still_listed():
    state = {"1218118170820306": "ed1b7ea6"}

    assert ts.reconcile(state, {"ed1b7ea6", "3070acad"}) == state


def test_reconcile_forgets_a_gid_whose_session_is_gone():
    state = {"1218118170820306": "ed1b7ea6", "1217000000000001": "deadbeef"}

    assert ts.reconcile(state, {"ed1b7ea6"}) == {"1218118170820306": "ed1b7ea6"}


# --- plan ----------------------------------------------------------------


def test_plan_spawns_a_task_with_no_live_session():
    rows = ts.plan([TASK], state={})

    assert [(r.gid, r.action, r.session_id) for r in rows] == [("1218118170820306", "spawn", None)]


def test_plan_skips_a_task_that_already_has_a_live_session():
    rows = ts.plan([TASK], state={"1218118170820306": "ed1b7ea6"})

    assert [(r.action, r.session_id) for r in rows] == [("already open", "ed1b7ea6")]


def test_plan_gives_every_row_a_ref_unique_within_the_run():
    other = dict(TASK, task_gid="1217000000000015", name="Something else")

    rows = ts.plan([TASK, other], state={})

    assert len({r.ref for r in rows}) == 2


# --- live_session_ids ----------------------------------------------------


def test_live_session_ids_reads_the_agents_listing():
    listing = json.dumps(
        [
            {"id": "ed1b7ea6", "kind": "background", "name": "gws x"},
            {"sessionId": "2f12a14e-30d4-478f-a0ed-8c38be4fe60d", "kind": "interactive"},
        ]
    )

    def runner(argv):
        return subprocess.CompletedProcess(argv, 0, stdout=listing, stderr="")

    assert ts.live_session_ids(runner=runner) == {"ed1b7ea6", "2f12a14e"}


def test_live_session_ids_is_empty_when_the_agents_call_fails():
    def boom(argv):
        raise OSError("claude not found")

    assert ts.live_session_ids(runner=boom) == set()


# --- state file ----------------------------------------------------------


def test_state_round_trips_through_the_file(tmp_path):
    path = tmp_path / "task-sessions.json"

    ts.save_state(path, {"1218118170820306": "ed1b7ea6"})

    assert ts.load_state(path) == {"1218118170820306": "ed1b7ea6"}


def test_load_state_is_empty_when_the_file_does_not_exist(tmp_path):
    assert ts.load_state(tmp_path / "nope.json") == {}


def test_load_state_is_empty_when_the_file_is_corrupt(tmp_path):
    path = tmp_path / "task-sessions.json"
    path.write_text("{not json")

    assert ts.load_state(path) == {}

"""Tests for scripts/task_tree.py — the subtask tree the task-lister draws.

The fetch is injected, so these walk an in-memory tree: ordering, nesting
connectors, the overdue flag, and refs drawn from the whole tree.
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("task_tree", SCRIPTS / "task_tree.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


task_tree = _load()
task_ref = task_tree.task_ref

TODAY = date(2026, 10, 26)


def _row(gid, name, due=None, completed=False):
    return {"task_gid": gid, "name": name, "due_on": due, "completed": completed}


TASKS = {
    "1": {
        "gid": "1",
        "name": "Party",
        "due_on": "2026-12-05",
        "project": "Ben's Board",
        "section": "Soon",
        "parent": None,
        "subtasks": [
            _row("13", "Stage", "2026-12-04"),
            _row("11", "Pick date", "2026-10-09", completed=True),
            _row("14", "Someday"),
            _row("12", "Plan setup", "2026-10-24"),
        ],
    },
    "11": {"subtasks": []},
    "12": {"subtasks": [_row("121", "Measure garage", "2026-10-30")]},
    "13": {"subtasks": []},
    "14": {"subtasks": []},
    "121": {"subtasks": []},
}


def _render():
    root, children = task_tree.walk("1", lambda gid: TASKS[gid])
    return task_tree.render(root, children, TODAY)


def test_walk_reaches_grandchildren():
    _, children = task_tree.walk("1", lambda gid: TASKS[gid])
    assert [r["task_gid"] for r in children["12"]] == ["121"]


def test_root_line_carries_weekday_due_and_location():
    assert _render().splitlines()[0] == "Party  (Sat Dec 5) · Ben's Board/Soon"


def test_siblings_sort_by_due_with_undated_last():
    names = [line.split("  ")[1] for line in _render().splitlines()[1:6] if line.startswith(("├", "└"))]
    assert names == ["Pick date", "Plan setup", "Stage", "Someday"]


def test_nested_rows_use_continuation_bar_and_last_connector():
    lines = _render().splitlines()
    refs = task_ref.assign(["11", "12", "13", "14", "121"])
    assert any(line.startswith(f"│   └── [ ] {refs['121']}  Measure garage") for line in lines)
    assert any(line.startswith(f"└── [ ] {refs['14']}  Someday") for line in lines)


def test_completed_is_checked_and_open_past_due_is_flagged():
    lines = _render().splitlines()
    pick = next(line for line in lines if "Pick date" in line)
    plan = next(line for line in lines if "Plan setup" in line)
    assert "[x]" in pick and "overdue" not in pick
    assert plan.endswith("Oct 24  ← overdue")


def test_due_column_is_aligned_across_depths():
    rows = [line for line in _render().splitlines() if line.startswith(("├", "└", "│"))]
    cols = {line.index(" Oct") if " Oct" in line else line.index(" Dec") if " Dec" in line else line.index(" —") for line in rows}
    assert len(cols) == 1


def test_refs_map_covers_every_row():
    out = _render()
    refs_line = out.splitlines()[-1]
    assert refs_line.startswith("refs: ")
    assert {pair.split("=")[1] for pair in refs_line[6:].split("  ")} == {"11", "12", "13", "14", "121"}


def test_no_subtasks():
    root = {"gid": "9", "name": "Solo", "subtasks": []}
    out = task_tree.render(root, {"9": []}, TODAY)
    assert out == "Solo · —\n(no subtasks)"

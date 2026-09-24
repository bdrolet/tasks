import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "task_next.py"
sys.path.insert(0, str(SCRIPT.parent))
spec = importlib.util.spec_from_file_location("task_next", SCRIPT)
tn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tn)

T = lambda gid, **kw: {  # noqa: E731
    "task_gid": gid,
    "name": f"[P1] {gid}",
    "project": "Inbox",
    "permalink_url": f"u/{gid}",
    "due_on": "2026-09-30",
    "points": 2,
    "score": 1.5,
    "position": 1,
    "rank": 1,
    "bucket": "next",
    "effective_due": "2026-09-30",
    "soft": False,
    "override": {},
    **kw,
}


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake(method, path, body=None, params=None):
        calls.append((method, path, body, params))
        if path == "/next":
            return {
                "today": "2026-09-23",
                "next": [T("a"), T("b", rank=2, position=2)],
                "overcommitted": [T("c", rank=None, position=3, overcommitted=True)],
                "stale": [],
                "nudge": [],
                "unenriched": 0,
            }
        if path == "/ranking":
            return {"today": "2026-09-23", "total": 2, "tasks": [T("a"), T("b", position=2)]}
        if path == "/calibrate":
            return {
                "projects": [
                    {
                        "project": "Inbox",
                        "completed": 2,
                        "mean_cycle_days_per_point": 1.5,
                        "median_cycle_days_per_point": 1.5,
                        "mean_points_ratio": 0.5,
                        "deferred_histogram": {"0": 2},
                    }
                ],
                "overall": {
                    "completed": 2,
                    "mean_cycle_days_per_point": 1.5,
                    "median_cycle_days_per_point": 1.5,
                    "mean_points_ratio": 0.5,
                    "deferred_histogram": {"0": 2},
                },
            }
        return {"ok": True}

    monkeypatch.setattr(tn, "_api", fake)
    return calls


def test_default_prints_ref_first_blocks(api, capsys):
    assert tn.main([]) == 0
    out = capsys.readouterr().out
    assert "## Next" in out and "## Overcommitted" in out
    assert api[0] == ("POST", "/next", {"explain": False}, None)
    ref_a = tn.task_ref.ref("a")
    assert out.splitlines()[2].startswith(f"{ref_a}\t")


def test_flags_are_passed(api):
    tn.main(["--energy", "deep", "--n", "3", "--explain"])
    assert api[0][2] == {"energy": "deep", "n": 3, "explain": True}


def test_resolve_ref_and_gid():
    tasks = [T("1218118170820306"), T("1218118170820307")]
    assert tn.resolve(tn.task_ref.ref("1218118170820306"), tasks) == "1218118170820306"
    assert tn.resolve("1218118170820307", tasks) == "1218118170820307"
    with pytest.raises(SystemExit):
        tn.resolve("zzz", tasks)


def test_write_subcommands_hit_the_right_endpoints(api):
    gid = "1218118170820306"
    tn.main(["start", gid])
    tn.main(["points", gid, "3"])
    tn.main(["pin", gid, "2"])
    tn.main(["unpin", gid])
    tn.main(["snooze", gid, "2026-09-30"])
    tn.main(["override", gid, "impact=high", "waiting_on="])
    writes = [c for c in api if c[0] in ("PATCH", "PUT")]
    assert writes[0][1] == f"/tasks/{gid}" and "started_at" in writes[0][2]
    assert writes[1][2] == {"story_points": 3}
    assert writes[2] == ("PUT", f"/tasks/{gid}/overrides", {"pinned_rank": 2}, None)
    assert writes[3][2] == {"pinned_rank": None}
    assert writes[4][2] == {"snooze_until": "2026-09-30"}
    assert writes[5][2] == {"impact": "high", "waiting_on": None}


def test_calibrate_renders_table(api, capsys):
    tn.main(["calibrate"])
    out = capsys.readouterr().out
    assert "Inbox" in out and "1.50" in out


def test_ranking_all_forwards_explain(api):
    tn.main(["ranking", "--all", "--explain"])
    ranking_calls = [c for c in api if c[1] == "/ranking"]
    assert len(ranking_calls) == 4
    assert all(c[3]["explain"] == "true" for c in ranking_calls)


def test_global_explain_survives_subcommand_either_order(api):
    tn.main(["--explain", "ranking"])
    assert api[-1][3]["explain"] == "true"
    tn.main(["ranking", "--explain"])
    assert api[-1][3]["explain"] == "true"


def test_override_rejects_unknown_field(api):
    with pytest.raises(SystemExit):
        tn.main(["override", "1218118170820306", "colour=red"])


def test_write_ref_resolves_against_the_listing_before_the_ranking(monkeypatch):
    real_ref = tn.task_ref.ref

    def colliding(gid, salt=0):
        # "0" (completed) and "a" (listed) share a ref; "0" sorts first, so
        # over the whole ranking it would win the ref and "a" would be salted.
        return "xyz" if gid in ("0", "a") and salt == 0 else real_ref(gid, salt)

    monkeypatch.setattr(tn.task_ref, "ref", colliding)
    calls = []

    def fake(method, path, body=None, params=None):
        calls.append((method, path, body, params))
        if path == "/next":
            return {"today": "2026-09-23", "next": [T("a")], "overcommitted": [], "stale": []}
        if path == "/ranking":
            if params["bucket"] == "excluded":
                return {"tasks": [T("0", bucket="excluded:completed", rank=None)]}
            return {"tasks": [T("a")] if params["bucket"] == "next" else []}
        return {"ok": True}

    monkeypatch.setattr(tn, "_api", fake)
    tn.main(["pin", "xyz", "1"])
    assert calls[-1] == ("PUT", "/tasks/a/overrides", {"pinned_rank": 1}, None)
    assert not [c for c in calls if c[1] == "/ranking"]  # found in the listing


def test_all_tasks_skips_completed_rows(monkeypatch):
    def fake(method, path, body=None, params=None):
        if params["bucket"] == "excluded":
            return {
                "tasks": [T("done", bucket="excluded:completed"), T("p", bucket="excluded:parent")]
            }
        return {"tasks": []}

    monkeypatch.setattr(tn, "_api", fake)
    assert [t["task_gid"] for t in tn._all_tasks()] == ["p"]


def test_write_ref_falls_back_to_the_ranking(api):
    ref_z = tn.task_ref.ref("z")
    # "z" is in neither /next list in the fixture, so resolution must reach
    # the ranking — which in the fixture has no "z" either.
    with pytest.raises(SystemExit):
        tn.main(["unpin", ref_z])
    assert any(c[1] == "/ranking" for c in api)

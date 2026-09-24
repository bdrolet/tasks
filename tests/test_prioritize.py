from datetime import date, datetime, timedelta, timezone

import pytest

from models.prioritize import Enrichment, Overrides, Stats, TaskFacts
from services import prioritize as pz
from services import prioritize_config as pc

CFG = pc.load()
TODAY = date(2026, 9, 23)
TS = datetime(2026, 9, 1, tzinfo=timezone.utc)


def facts(gid, name="[P1] Do thing", project="Inbox", due_on=None, points=None, **kw):
    base = dict(
        gid=gid,
        project_gid="p-" + project,
        project_name=project,
        parent_gid=None,
        name=name,
        permalink_url=f"https://app.asana.com/0/0/{gid}",
        priority=pz.parse_priority(name),
        due_on=due_on,
        due_at=None,
        start_on=None,
        started_at=None,
        story_points=points,
        points_estimated=None,
        completed=False,
        completed_at=None,
        created_at=TS,
        modified_at=TS,
        tags=(),
        dependencies=(),
        dependents=(),
        num_open_subtasks=0,
        content_hash="h",
    )
    base.update(kw)
    return TaskFacts(**base)


def enr(**kw):
    d = Enrichment.DEFAULT.__dict__ | {"unenriched": False} | kw
    return Enrichment(**d)


def run(fs, enrichments=None, overrides=None, stats=None, today=TODAY):
    return pz.score_set(fs, enrichments or {}, overrides or {}, stats or {}, CFG, today)


def test_parse_priority():
    assert pz.parse_priority("[P0] x") == "P0"
    assert pz.parse_priority("plain") is None


def test_same_due_shorter_slack_is_more_urgent():
    # The spec's first fixture: same due date, different effort. Urgency is
    # slack-driven, so the bigger task is the more urgent one even though the
    # two share a due date — that is the property this test pins. The final
    # score still divides by effort (WSJF), so the *smaller* task can outrank
    # it; that is by design, not a defect.
    a = facts("a", due_on=TODAY + timedelta(days=5), points=1)
    b = facts("b", due_on=TODAY + timedelta(days=5), points=5)
    s = run([a, b]).by_gid()
    assert s["b"].components["slack"] < s["a"].components["slack"]
    assert s["b"].components["effective_slack"] < s["a"].components["effective_slack"]
    assert s["b"].components["U"] > s["a"].components["U"]


def test_negative_slack_saturates_urgency_and_flags_overcommitted():
    a = facts("a", due_on=TODAY + timedelta(days=1), points=8)
    b = facts("b", due_on=TODAY + timedelta(days=1), points=8)
    s = run([a, b]).by_gid()
    assert s["a"].components["effective_slack"] < 0 and s["a"].overcommitted
    assert s["b"].components["U"] > 0.99


def test_soft_deadline_caps_urgency():
    a = facts(
        "a", due_on=None, points=1, created_at=TS - timedelta(days=60)
    )  # P1 horizon long past
    s = run([a]).by_gid()["a"]
    assert s.components["soft"] is True
    assert s.components["U"] == CFG.soft_cap


def test_inferred_due_is_soft_and_needs_confidence():
    a = facts("a", points=1)
    hi = enr(due_date_inferred=TODAY + timedelta(days=2), due_date_inferred_confidence="high")
    lo = enr(due_date_inferred=TODAY + timedelta(days=2), due_date_inferred_confidence="low")
    assert (
        run([a], {"a": hi}).by_gid()["a"].components["effective_due"]
        == (TODAY + timedelta(days=2)).isoformat()
    )
    assert (
        run([a], {"a": lo}).by_gid()["a"].components["effective_due"]
        != (TODAY + timedelta(days=2)).isoformat()
    )


def test_no_dates_no_prefix_still_scores():
    a = facts(
        "a",
        name="plain title",
        points=None,
        created_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
    )  # noon UTC = same date in LA
    s = run([a]).by_gid()["a"]
    assert s.bucket == "next" and s.score is not None
    assert s.components["priority"] == "P2" and s.components["points_source"] == "default"
    assert s.components["effective_due"] == (date(2026, 9, 22) + timedelta(days=45)).isoformat()


def test_no_due_of_any_kind_when_horizon_missing():
    cfg_no_horizon = pc.Config(**(CFG.__dict__ | {"horizon_days": {}}))
    a = facts("a", points=1)
    s = pz.score_set([a], {}, {}, {}, cfg_no_horizon, TODAY).by_gid()["a"]
    assert s.components["effective_due"] is None and s.components["U"] == CFG.no_due_urgency


def test_future_modified_at_clamps_aging():
    a = facts("a", points=1, modified_at=datetime(2030, 1, 1, tzinfo=timezone.utc))
    s = run([a]).by_gid()["a"]
    assert s.components["days_stale"] == 0 and s.components["A"] == 0


def test_blocked_parent_and_waiting_are_excluded():
    dep = facts("dep", points=1)
    blocked = facts("b", points=1, dependencies=("dep",))
    parent = facts("p", points=1, num_open_subtasks=2)
    waiting = facts("w", points=1)
    done_dep = facts("dd", points=1, completed=True)
    unblocked = facts("u", points=1, dependencies=("dd",))
    s = run(
        [dep, blocked, parent, waiting, done_dep, unblocked], {"w": enr(waiting_on="the lawyer")}
    ).by_gid()
    assert s["b"].bucket == "excluded:blocked"
    assert s["p"].bucket == "excluded:parent"
    assert s["w"].bucket == "nudge"
    assert s["dd"].bucket == "excluded:completed"
    assert s["u"].bucket == "next"


def test_unblock_bonus_counts_open_dependents():
    a = facts("a", points=1, dependents=("x", "y", "z", "q"))
    others = [facts(g, points=1, dependencies=("a",)) for g in "xyz"] + [
        facts("q", points=1, completed=True)
    ]
    s = run([a, *others]).by_gid()["a"]
    assert s.components["B"] == pytest.approx(0.9)  # 3 open dependents * 0.3


def test_tag_beats_override_beats_model():
    a = facts("a", points=1, tags=("impact:high", "energy:deep"))
    e = enr(impact="low", energy="shallow", waiting_on=None)
    o = Overrides(fields={"impact": "medium", "waiting_on": "vendor"})
    eff = pz.effective(a, e, o, CFG)
    assert eff.impact == "high" and eff.energy == "deep" and eff.waiting_on == "vendor"
    assert pz.effective(a, e, Overrides.NONE, CFG).waiting_on is None


def test_effective_ignores_malformed_stored_due_date_inferred():
    a = facts("a", points=1)
    o = Overrides(fields={"due_date_inferred": "not-a-date"})
    eff = pz.effective(a, enr(), o, CFG)
    assert eff.due_date_inferred is None


def test_points_precedence_and_low_confidence_multiplier():
    field = facts("f", points=2, points_estimated=5)
    est = facts("e", points=None, points_estimated=5)
    none = facts("n")
    assert pz.effective(field, enr(), Overrides.NONE, CFG).points_source == "field"
    assert pz.effective(est, enr(points_confidence="low"), Overrides.NONE, CFG).points == 5
    s = run(
        [field, est, none], {"e": enr(points_confidence="low"), "f": enr(points_confidence="low")}
    ).by_gid()
    assert s["f"].components["effort_days"] == 2 / CFG.points_per_day  # field: no multiplier
    assert (
        s["e"].components["effort_days"] == 5 / CFG.points_per_day * CFG.low_confidence_multiplier
    )
    assert s["n"].components["points"] == CFG.default_points


def test_diversity_penalty_mixes_projects():
    heavy = [
        facts(f"c{i}", project="Consulting", points=1, due_on=TODAY + timedelta(days=3))
        for i in range(8)
    ]
    other = [
        facts(f"f{i}", project="Family", points=1, due_on=TODAY + timedelta(days=4))
        for i in range(2)
    ]
    scored = run(heavy + other)
    top5 = pz.select(scored.next(), CFG)
    assert {t.project_name for t in top5} == {"Consulting", "Family"}
    assert (
        scored.next()[0].project_name == "Consulting"
    )  # the ranking itself is untouched by selection


def test_selection_respects_capacity_and_n():
    fs = [facts(f"t{i}", points=3, due_on=TODAY + timedelta(days=i + 1)) for i in range(6)]
    scored = run(fs)
    assert sum(t.points for t in pz.select(scored.next(), CFG)) >= CFG.points_per_day
    assert len(pz.select(scored.next(), CFG)) == 2  # 3 + 3 fills 5
    assert len(pz.select(scored.next(), CFG, n=1)) == 1


def test_energy_flag_demotes_mismatches():
    deep = facts("d", points=1, due_on=TODAY + timedelta(days=2))
    shallow = facts("s", points=1, due_on=TODAY + timedelta(days=2))
    scored = run([deep, shallow], {"d": enr(energy="deep"), "s": enr(energy="shallow")})
    assert pz.select(scored.next(), CFG, n=1, energy="shallow")[0].gid == "s"
    assert pz.select(scored.next(), CFG, n=1, energy="deep")[0].gid == "d"


def test_pin_holds_position_over_score_and_capacity():
    low = facts("low", points=8, due_on=TODAY + timedelta(days=60))
    highs = [facts(f"h{i}", points=1, due_on=TODAY + timedelta(days=1)) for i in range(5)]
    scored = run([low, *highs], overrides={"low": Overrides(pinned_rank=1)})
    assert scored.by_gid()["low"].position == 1
    picked = pz.select(scored.next(), CFG)
    assert picked[0].gid == "low" and len(picked) == 6  # pins count toward neither n nor capacity


def test_two_pins_same_position_order_by_score():
    a = facts("a", points=1, due_on=TODAY + timedelta(days=1))
    b = facts("b", points=1, due_on=TODAY + timedelta(days=30))
    scored = run([a, b], overrides={"a": Overrides(pinned_rank=1), "b": Overrides(pinned_rank=1)})
    assert [t.gid for t in scored.next()] == ["a", "b"]


def test_two_pins_same_rank_select_and_rank_follow_score():
    a = facts("a", points=1, due_on=TODAY + timedelta(days=1))
    b = facts("b", points=1, due_on=TODAY + timedelta(days=30))
    c = facts("c", points=1, due_on=TODAY + timedelta(days=10))
    scored = run(
        [a, b, c], overrides={"a": Overrides(pinned_rank=1), "b": Overrides(pinned_rank=1)}
    )
    assert [t.gid for t in pz.select(scored.next(), CFG)] == ["a", "b", "c"]
    assert (scored.by_gid()["a"].rank, scored.by_gid()["b"].rank, scored.by_gid()["c"].rank) == (
        1,
        2,
        3,
    )


def test_pinned_blocked_task_still_appears_flagged():
    dep = facts("dep", points=1)
    b = facts("b", points=1, dependencies=("dep",))
    s = run([dep, b], overrides={"b": Overrides(pinned_rank=1)}).by_gid()["b"]
    assert s.bucket == "next" and s.components["pinned_despite"] == "blocked"


def test_snoozed_is_in_no_list_until_its_date():
    a = facts("a", points=1)
    snoozed = run([a], overrides={"a": Overrides(snooze_until=TODAY + timedelta(days=1))})
    assert snoozed.by_gid()["a"].bucket == "snoozed"
    assert all(not lst for lst in pz.side_lists(snoozed).values())
    back = run([a], overrides={"a": Overrides(snooze_until=TODAY)})
    assert back.by_gid()["a"].bucket == "next"


def test_stale_rules_each_trigger():
    old_p3 = facts("o", name="[P3] old", points=1, modified_at=TS - timedelta(days=60))
    deferred = facts("d", points=1)
    soft_past = facts("s", name="[P0] past", points=1, created_at=TS - timedelta(days=10))
    fresh = facts(
        "f",
        points=1,
        due_on=TODAY + timedelta(days=10),
        modified_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
    )
    s = run([old_p3, deferred, soft_past, fresh], stats={"d": Stats(times_deferred=5)}).by_gid()
    assert s["o"].stale and s["o"].stale_reason == "aged"
    assert s["d"].stale and s["d"].stale_reason == "deferred"
    assert s["s"].stale and s["s"].stale_reason == "soft_due_passed"
    assert not s["f"].stale


def test_nudge_sorted_by_days_stale_desc():
    a = facts("a", points=1, modified_at=TS - timedelta(days=1))
    b = facts("b", points=1, modified_at=TS - timedelta(days=10))
    scored = run([a, b], {"a": enr(waiting_on="x"), "b": enr(waiting_on="y")})
    assert [t.gid for t in pz.side_lists(scored)["nudge"]] == ["b", "a"]


def test_positions_are_total_over_the_set():
    a = facts("a", points=1)
    w = facts("w", points=1)
    scored = run([a, w], {"w": enr(waiting_on="x")})
    assert sorted(t.position for t in scored.tasks) == [1, 2]
    assert scored.by_gid()["a"].position == 1

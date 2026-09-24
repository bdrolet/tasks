from dataclasses import replace as replace_facts
from datetime import date, datetime, timedelta, timezone

import pytest

from models.prioritize import Enrichment, Overrides, Stats, TaskFacts
from services import prioritize as pz
from services import prioritize_config as pc

CFG = pc.load()
TODAY = date(2026, 9, 23)
TS = datetime(2026, 9, 1, tzinfo=timezone.utc)


def facts(gid, name="[P1] Do thing", project="Work", due_on=None, points=None, **kw):
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


def run(fs, enrichments=None, overrides=None, stats=None, today=TODAY, last_offered=None):
    return pz.score_set(
        fs,
        enrichments or {},
        overrides or {},
        stats or {},
        CFG,
        today,
        project_last_offered=last_offered,
    )


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
    assert s.components["soft"] is True and s.components["due_source"] == "horizon"
    assert s.components["U"] == CFG.soft_cap_horizon


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
    soft_past = facts("s", name="[P0] past", points=1)
    fresh = facts(
        "f",
        points=1,
        due_on=TODAY + timedelta(days=10),
        modified_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
    )
    past = enr(due_date_inferred=TODAY - timedelta(days=2), due_date_inferred_confidence="high")
    s = run(
        [old_p3, deferred, soft_past, fresh], {"s": past}, stats={"d": Stats(times_deferred=5)}
    ).by_gid()
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


def test_parent_whose_only_subtask_row_is_completed_is_next():
    # The parent's stored count is stale (gathered while the subtask was open);
    # the subtask's own row says it is done.
    parent = facts("p", points=1, num_open_subtasks=1)
    sub = facts("s", points=1, parent_gid="p", completed=True)
    assert run([parent, sub]).by_gid()["p"].bucket == "next"


def test_parent_count_comes_from_open_subtask_rows_when_present():
    parent = facts("p", points=1, num_open_subtasks=0)
    sub = facts("s", points=1, parent_gid="p")
    assert run([parent, sub]).by_gid()["p"].bucket == "excluded:parent"


def test_parent_without_subtask_rows_keeps_stored_count():
    parent = facts("p", points=1, num_open_subtasks=2)
    assert run([parent]).by_gid()["p"].bucket == "excluded:parent"


def test_pin_takes_its_position_among_unpinned():
    us = [facts(f"u{i}", points=1, due_on=TODAY + timedelta(days=i)) for i in (1, 2, 3)]
    pinned = facts("pin", points=8, due_on=TODAY + timedelta(days=60))
    scored = run([*us, pinned], overrides={"pin": Overrides(pinned_rank=3)})
    by = scored.by_gid()
    order = sorted(("u1", "u2", "u3"), key=lambda g: by[g].position)
    assert order == ["u1", "u2", "u3"]
    assert [t.gid for t in sorted(scored.next(), key=lambda t: t.position)] == [
        "u1",
        "u2",
        "pin",
        "u3",
    ]
    assert by["pin"].position == 3
    assert [t.gid for t in pz.select(scored.next(), CFG)] == [
        t.gid for t in sorted(scored.next(), key=lambda t: t.rank or 99) if t.rank
    ]
    assert by["pin"].rank == 3


# ---- tuning: sources, exclusion, hard-only feasibility, must-do, starvation ----


def test_due_source_is_recorded_per_kind():
    hard = facts("h", points=1, due_on=TODAY + timedelta(days=4))
    inferred = facts("i", points=1)
    horizon = facts("z", points=1)
    cfg_no_horizon = pc.Config(**(CFG.__dict__ | {"horizon_days": {}}))
    s = run(
        [hard, inferred, horizon],
        {
            "i": enr(
                due_date_inferred=TODAY + timedelta(days=4), due_date_inferred_confidence="medium"
            )
        },
    ).by_gid()
    assert (s["h"].components["due_source"], s["h"].components["soft"]) == ("hard", False)
    assert (s["i"].components["due_source"], s["i"].components["soft"]) == ("inferred", True)
    assert (s["z"].components["due_source"], s["z"].components["soft"]) == ("horizon", True)
    none = pz.score_set([horizon], {}, {}, {}, cfg_no_horizon, TODAY).by_gid()["z"]
    assert none.components["due_source"] == "none"


def test_excluded_project_is_bucketed_and_a_pin_cannot_override():
    inbox = facts("i", project="Inbox", points=1, due_on=TODAY)
    pinned = facts("p", project="Inbox", points=1)
    work = facts("w", points=1)
    s = run([inbox, pinned, work], overrides={"p": Overrides(pinned_rank=1)})
    by = s.by_gid()
    assert by["i"].bucket == "excluded:project" and by["p"].bucket == "excluded:project"
    assert by["i"].rank is None and by["p"].rank is None
    assert [t.gid for t in s.next()] == ["w"]
    assert [t.gid for t in pz.select(s.next(), CFG)] == ["w"]


def test_excluded_project_check_follows_completed_and_snoozed():
    done = facts("d", project="Inbox", points=1, completed=True)
    snoozed = facts("s", project="Inbox", points=1)
    by = run(
        [done, snoozed], overrides={"s": Overrides(snooze_until=TODAY + timedelta(days=2))}
    ).by_gid()
    assert by["d"].bucket == "excluded:completed" and by["s"].bucket == "snoozed"


def test_feasibility_runs_over_hard_dates_only():
    a = facts("a", points=3, due_on=TODAY + timedelta(days=3))
    b = facts("b", points=3, due_on=TODAY + timedelta(days=3))
    soft = facts("s", points=3, created_at=TS - timedelta(days=90))  # horizon long past
    by = run([a, b, soft]).by_gid()
    starts = sorted(by[g].components["simulated_start"] for g in "ab")
    assert starts[0] == 0 and starts[1] > 0
    assert by["s"].components["simulated_start"] is None and by["s"].overcommitted is False
    c = by["s"].components
    assert c["slack"] == c["effective_slack"] == c["days_until_due"] - c["effort_days"]


def test_hundreds_of_undated_tasks_do_not_overcommit_a_hard_one():
    undated = [facts(f"u{i}", points=5, created_at=TS - timedelta(days=200)) for i in range(50)]
    hard = facts("h", points=1, due_on=TODAY + timedelta(days=10))
    scored = run([*undated, hard])
    assert pz.side_lists(scored)["overcommitted"] == []
    assert scored.by_gid()["h"].components["simulated_start"] == 0


def test_urgency_caps_by_source():
    past = TODAY - timedelta(days=20)
    inferred = facts("i", points=1)
    horizon = facts("z", points=1, created_at=TS - timedelta(days=90))
    hard_a = facts("a", points=8, due_on=TODAY + timedelta(days=1))
    hard_b = facts("b", points=8, due_on=TODAY + timedelta(days=1))
    by = run(
        [inferred, horizon, hard_a, hard_b],
        {"i": enr(due_date_inferred=past, due_date_inferred_confidence="high")},
    ).by_gid()
    assert by["i"].components["U"] == pytest.approx(CFG.soft_cap_inferred)
    assert by["z"].components["U"] == pytest.approx(CFG.soft_cap_horizon)
    assert by["i"].components["U"] <= 0.6 and by["z"].components["U"] <= 0.4
    assert by["b"].components["effective_slack"] < 0 and by["b"].components["U"] > 0.99


def test_past_horizon_is_not_stale_but_past_inferred_is():
    horizon = facts("z", name="[P0] old", points=1, created_at=TS - timedelta(days=30))
    inferred = facts("i", points=1)
    by = run(
        [horizon, inferred],
        {
            "i": enr(
                due_date_inferred=TODAY - timedelta(days=1), due_date_inferred_confidence="high"
            )
        },
    ).by_gid()
    assert by["z"].components["days_until_due"] < 0 and not by["z"].stale
    assert by["i"].stale and by["i"].stale_reason == "soft_due_passed"


def test_hard_due_today_is_selected_first_beyond_n_and_consumes_capacity():
    must = facts("m", name="[P3] file it", points=4, due_on=TODAY)
    highs = [
        facts(f"h{i}", name="[P0] big", points=1, due_on=TODAY + timedelta(days=3))
        for i in range(4)
    ]
    scored = run([must, *highs])
    by = scored.by_gid()
    assert all((by[f"h{i}"].score or 0) > (by["m"].score or 0) for i in range(4))
    picked = pz.select(scored.next(), CFG)
    assert picked[0].gid == "m"
    assert [t.gid for t in picked][1:] and len(picked) == 2  # 4 + 1 fills 5
    assert [t.gid for t in pz.select(scored.next(), CFG, n=1)] == ["m"]
    assert by["m"].rank == 1


def test_every_hard_must_do_is_placed_even_past_n_and_capacity():
    musts = [
        facts(f"m{i}", name="[P3] x", points=3, due_on=TODAY + timedelta(days=d))
        for i, d in enumerate((-2, 0, 1))
    ]
    other = facts("o", name="[P0] y", points=1, due_on=TODAY + timedelta(days=5))
    picked = pz.select(run([*musts, other]).next(), CFG, n=1)
    assert {t.gid for t in picked} == {"m0", "m1", "m2"}


def test_must_do_ignores_soft_dates_inside_the_window():
    inferred = facts("i", name="[P3] x", points=1)
    top = facts("t", name="[P0] y", points=1, due_on=TODAY + timedelta(days=5))
    scored = run(
        [inferred, top],
        {"i": enr(due_date_inferred=TODAY, due_date_inferred_confidence="high")},
    )
    assert [t.gid for t in pz.select(scored.next(), CFG, n=1)] == ["t"]


def test_starvation_boost_favours_the_longer_unpicked_project():
    a = facts("a", project="A", points=1)  # undated: identical scores
    b = facts("b", project="B", points=1)  # undated: identical scores
    c = facts("c", project="C", points=1)  # undated: identical scores
    last = {"A": TODAY - timedelta(days=1), "B": TODAY - timedelta(days=5)}
    scored = run([a, b, c], last_offered=last)
    by = scored.by_gid()
    assert by["a"].score == by["b"].score
    assert by["a"].components["starvation_boost"] == pytest.approx(0.1)
    assert by["a"].components["days_since_project_offered"] == 1
    assert by["b"].components["starvation_boost"] == pytest.approx(0.5)
    assert by["c"].components["starvation_boost"] == CFG.starvation_max_boost
    assert by["c"].components["days_since_project_offered"] is None
    assert [t.gid for t in pz.select([by["a"], by["b"]], CFG, n=1)] == ["b"]
    # the boost is a selection input only: score and position are untouched
    assert by["b"].score == by["c"].score


def test_equal_scores_across_projects_still_mix_the_top():
    fs = [
        facts(f"{p}{i}", project=p, points=1)  # undated: identical scores
        for p in ("A", "B")
        for i in range(3)
    ]
    picked = pz.select(run(fs).next(), CFG, n=2)
    assert {t.project_name for t in picked} == {"A", "B"}


def test_hard_p1_due_today_outscores_horizon_p0():
    p0 = facts("z", name="[P0] made up", points=1, created_at=TS - timedelta(days=30))
    p1 = facts("h", name="[P1] real", points=1, due_on=TODAY)
    by = run([p0, p1]).by_gid()
    assert by["z"].components["U"] == pytest.approx(CFG.soft_cap_horizon)
    assert by["h"].components["U"] > 0.9
    assert (by["h"].score or 0) > (by["z"].score or 0)
    assert by["h"].position < by["z"].position


def test_same_day_or_future_offer_gives_no_boost_defensively():
    # The query excludes today's run; should a same-day (or clock-skewed
    # future) date reach the scorer anyway, it reads as "just offered".
    a = facts("a", project="A", points=1)
    b = facts("b", project="B", points=1)
    by = run([a, b], last_offered={"A": TODAY, "B": TODAY + timedelta(days=1)}).by_gid()
    assert by["a"].components["starvation_boost"] == 0
    assert by["b"].components["starvation_boost"] == 0


# D16 — subtasks inherit snoozed / blocked / waiting from their ancestors.


def test_child_of_snoozed_parent_is_snoozed():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run(
        [parent, child], overrides={"p": Overrides(snooze_until=TODAY + timedelta(days=3))}
    ).by_gid()["c"]
    assert s.bucket == "snoozed"
    assert s.components["inherited"] == {"state": "snoozed", "from": "p"}


def test_child_of_blocked_parent_is_blocked_until_the_dependency_completes():
    dep = facts("dep", points=1)
    parent = facts("p", points=1, dependencies=("dep",))
    child = facts("c", points=1, parent_gid="p")
    s = run([dep, parent, child]).by_gid()["c"]
    assert s.bucket == "excluded:blocked"
    assert s.components["inherited"] == {"state": "blocked", "from": "p"}
    done = run([replace_facts(dep, completed=True), parent, child]).by_gid()["c"]
    assert done.bucket == "next"
    assert done.components["inherited"] is None


def test_child_of_waiting_parent_is_a_nudge_under_the_parents_person():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    scored = run([parent, child], {"p": enr(waiting_on="the consulate")})
    s = scored.by_gid()["c"]
    assert s.bucket == "nudge"
    assert s.components["waiting_on"] == "the consulate"
    assert s.components["inherited"] == {"state": "waiting", "from": "p"}
    assert "c" in {t.gid for t in pz.side_lists(scored)["nudge"]}


def test_own_state_wins_over_inherited_and_is_not_marked_inherited():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"p": enr(waiting_on="A"), "c": enr(waiting_on="B")}).by_gid()["c"]
    assert s.bucket == "nudge"
    assert s.components["waiting_on"] == "B"
    assert s.components["inherited"] is None


def test_grandchild_inherits_through_two_levels():
    dep = facts("dep", points=1)
    gp = facts("gp", points=1, dependencies=("dep",))
    p = facts("p", points=1, parent_gid="gp")
    c = facts("c", points=1, parent_gid="p")
    s = run([dep, gp, p, c]).by_gid()["c"]
    assert s.bucket == "excluded:blocked"
    assert s.components["inherited"] == {"state": "blocked", "from": "gp"}


def test_pinned_child_of_blocked_parent_is_next_flagged():
    dep = facts("dep", points=1)
    parent = facts("p", points=1, dependencies=("dep",))
    child = facts("c", points=1, parent_gid="p")
    s = run([dep, parent, child], overrides={"c": Overrides(pinned_rank=1)}).by_gid()["c"]
    assert s.bucket == "next"
    assert s.components["pinned_despite"] == "blocked"
    assert s.components["inherited"] == {"state": "blocked", "from": "p"}


def test_pinned_child_of_snoozed_parent_stays_snoozed():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run(
        [parent, child],
        overrides={
            "p": Overrides(snooze_until=TODAY + timedelta(days=3)),
            "c": Overrides(pinned_rank=1),
        },
    ).by_gid()["c"]
    assert s.bucket == "snoozed"


def test_inheritance_walks_at_most_three_levels():
    root = facts("r", points=1)
    chain = [root]
    for i in range(1, 5):
        chain.append(facts(f"l{i}", points=1, parent_gid=chain[-1].gid))
    s = run(chain, overrides={"r": Overrides(snooze_until=TODAY + timedelta(days=3))}).by_gid()
    assert s["l3"].bucket == "snoozed"
    assert s["l3"].components["inherited"] == {"state": "snoozed", "from": "r"}
    assert s["l4"].bucket == "next"


def test_inheritance_stops_at_a_missing_ancestor_row():
    # gp (snoozed) -> p (no facts row) -> c: the walk stops at p, mid-chain.
    gp = facts("gp", points=1)
    c = facts("c", points=1, parent_gid="p")
    s = run([gp, c], overrides={"gp": Overrides(snooze_until=TODAY + timedelta(days=3))}).by_gid()
    assert s["c"].bucket == "next"
    assert s["c"].components["inherited"] is None


def test_completed_ancestor_passes_no_state_down():
    dep = facts("dep", points=1)
    gp = facts("gp", points=1, dependencies=("dep",))
    p = facts("p", points=1, parent_gid="gp", completed=True)
    c = facts("c", points=1, parent_gid="p")
    s = run(
        [dep, gp, p, c],
        {"p": enr(waiting_on="someone")},
        overrides={"p": Overrides(snooze_until=TODAY + timedelta(days=3))},
    ).by_gid()["c"]
    assert s.bucket == "next"
    assert s.components["inherited"] is None

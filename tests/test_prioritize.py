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
        [dep, blocked, parent, waiting, done_dep, unblocked],
        {"w": enr(waiting_on="the lawyer", waiting_confidence="high")},
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
    fs = [facts(f"t{i}", points=3, due_on=TODAY + timedelta(days=i + 10)) for i in range(6)]
    scored = run(fs)
    assert sum(t.points for t in pz.select(scored.next(), CFG)) >= CFG.points_per_day
    assert len(pz.select(scored.next(), CFG)) == 2  # 3 + 3 fills 5
    assert len(pz.select(scored.next(), CFG, n=1)) == 1


def test_energy_flag_demotes_mismatches():
    deep = facts("d", points=1, due_on=TODAY + timedelta(days=10))
    shallow = facts("s", points=1, due_on=TODAY + timedelta(days=10))
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
    scored = run(
        [a, b],
        {
            "a": enr(waiting_on="x", waiting_confidence="high"),
            "b": enr(waiting_on="y", waiting_confidence="high"),
        },
    )
    assert [t.gid for t in pz.side_lists(scored)["nudge"]] == ["b", "a"]


def test_positions_are_total_over_the_set():
    a = facts("a", points=1)
    w = facts("w", points=1)
    scored = run([a, w], {"w": enr(waiting_on="x", waiting_confidence="high")})
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
        facts(f"h{i}", name="[P0] big", points=1, due_on=TODAY + timedelta(days=10))
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
    other = facts("o", name="[P0] y", points=1, due_on=TODAY + timedelta(days=10))
    picked = pz.select(run([*musts, other]).next(), CFG, n=1)
    assert {t.gid for t in picked} == {"m0", "m1", "m2"}


def test_must_do_ignores_soft_dates_inside_the_window():
    inferred = facts("i", name="[P3] x", points=1)
    top = facts("t", name="[P0] y", points=1, due_on=TODAY + timedelta(days=10))
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


def test_child_of_tag_waiting_parent_is_a_nudge_under_the_parents_person():
    parent = facts("p", points=1, tags=("waiting:the consulate",))
    child = facts("c", points=1, parent_gid="p")
    scored = run([parent, child])
    s = scored.by_gid()["c"]
    assert s.bucket == "nudge"
    assert s.components["waiting_on"] == "the consulate"
    assert s.components["waiting_source"] == "tag"
    assert s.components["inherited"] == {"state": "waiting", "from": "p"}
    assert "c" in {t.gid for t in pz.side_lists(scored)["nudge"]}


def test_child_of_override_waiting_parent_is_a_nudge():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], overrides={"p": Overrides(fields={"waiting_on": "FTB"})}).by_gid()["c"]
    assert s.bucket == "nudge" and s.components["waiting_on"] == "FTB"
    assert s.components["waiting_source"] == "override"
    assert s.components["inherited"] == {"state": "waiting", "from": "p"}


def test_child_of_model_waiting_parent_is_not_waiting():
    parent = facts("p", points=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"p": enr(waiting_on="FTB", waiting_confidence="high")}).by_gid()
    # the parent's own wait still holds on the parent (it buckets excluded:parent
    # because it has an open child, but its own wait is recorded)
    assert s["p"].components["waiting_on"] == "FTB"
    assert s["p"].components["waiting_source"] == "model"
    assert s["c"].bucket == "next"
    assert s["c"].components["waiting_on"] is None
    assert s["c"].components["inherited"] is None


def test_own_state_wins_over_inherited_and_is_not_marked_inherited():
    parent = facts("p", points=1, tags=("waiting:A",))
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"c": enr(waiting_on="B", waiting_confidence="high")}).by_gid()["c"]
    assert s.bucket == "nudge"
    assert s.components["waiting_on"] == "B"
    assert s.components["waiting_source"] == "model"
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
        {"p": enr(waiting_on="someone", waiting_confidence="high")},
        overrides={"p": Overrides(snooze_until=TODAY + timedelta(days=3))},
    ).by_gid()["c"]
    assert s.bucket == "next"
    assert s.components["inherited"] is None


def test_undated_child_inherits_the_parents_hard_due_date():
    # The Tasca return: the 30-day deadline sat on the parent, excluded as
    # `parent`; its one open subtask had no date and ranked on a horizon.
    parent = facts("p", due_on=TODAY - timedelta(days=7), points=1)
    child = facts("c", points=1, parent_gid="p")
    scored = run([parent, child])
    c = scored.by_gid()["c"].components
    assert c["effective_due"] == (TODAY - timedelta(days=7)).isoformat()
    assert c["due_source"] == "hard"
    assert c["due_from"] == "p"
    assert scored.by_gid()["c"].overcommitted
    assert "c" in {t.gid for t in pz.select(scored.next(), CFG)}


def test_childs_own_due_date_wins_over_the_parents():
    parent = facts("p", due_on=TODAY + timedelta(days=1), points=1)
    child = facts("c", due_on=TODAY + timedelta(days=10), points=1, parent_gid="p")
    c = run([parent, child]).by_gid()["c"].components
    assert c["effective_due"] == (TODAY + timedelta(days=10)).isoformat()
    assert c["due_from"] is None


def test_parents_hard_date_beats_the_childs_inferred_date():
    parent = facts("p", due_on=TODAY + timedelta(days=2), points=1)
    child = facts("c", points=1, parent_gid="p")
    inferred = enr(
        due_date_inferred=TODAY + timedelta(days=20), due_date_inferred_confidence="high"
    )
    c = run([parent, child], {"c": inferred}).by_gid()["c"].components
    assert c["effective_due"] == (TODAY + timedelta(days=2)).isoformat()
    assert c["due_source"] == "hard"


def test_grandchild_takes_the_nearest_dated_ancestor():
    gp = facts("gp", due_on=TODAY + timedelta(days=9), points=1)
    p = facts("p", due_on=TODAY + timedelta(days=4), points=1, parent_gid="gp")
    c = facts("c", points=1, parent_gid="p")
    skip = facts("s", points=1, parent_gid="u")
    u = facts("u", points=1, parent_gid="gp")
    by = run([gp, p, c, u, skip]).by_gid()
    assert by["c"].components["due_from"] == "p"
    assert by["s"].components["due_from"] == "gp"


def test_completed_parent_passes_no_due_date_down():
    parent = facts("p", due_on=TODAY - timedelta(days=3), points=1, completed=True)
    child = facts("c", points=1, parent_gid="p")
    c = run([parent, child]).by_gid()["c"].components
    assert c["due_source"] != "hard"
    assert c["due_from"] is None


def test_waiting_source_follows_precedence():
    a = facts("a", points=1)
    model = enr(waiting_on="the model", waiting_confidence="high")
    assert pz.effective(a, model, Overrides.NONE, CFG).waiting_source == "model"
    o = Overrides(fields={"waiting_on": "vendor"})
    e = pz.effective(a, model, o, CFG)
    assert (e.waiting_on, e.waiting_source) == ("vendor", "override")
    tagged = facts("t", points=1, tags=("waiting:the bank",))
    e = pz.effective(tagged, model, o, CFG)
    assert (e.waiting_on, e.waiting_source) == ("the bank", "tag")
    assert pz.effective(a, enr(), Overrides.NONE, CFG).waiting_source == "none"


def test_low_confidence_model_wait_is_not_a_wait():
    a = facts("a", points=1)
    for conf, bucket in (("low", "next"), ("medium", "nudge"), ("high", "nudge")):
        s = run([a], {"a": enr(waiting_on="someone", waiting_confidence=conf)}).by_gid()["a"]
        assert s.bucket == bucket, conf
        assert s.components["waiting_confidence"] == conf
    low = run([a], {"a": enr(waiting_on="someone", waiting_confidence="low")}).by_gid()["a"]
    assert low.components["waiting_on"] is None and low.components["waiting_source"] == "none"


def test_tag_and_override_waits_ignore_confidence():
    o = Overrides(fields={"waiting_on": "vendor"})
    a = facts("a", points=1)
    assert run([a], {"a": enr(waiting_confidence="low")}, {"a": o}).by_gid()["a"].bucket == "nudge"
    t = facts("t", points=1, tags=("waiting:bank",))
    assert run([t], {"t": enr(waiting_confidence="low")}).by_gid()["t"].bucket == "nudge"


def test_empty_override_means_not_waiting():
    a = facts("a", points=1)
    model = enr(waiting_on="Michael", waiting_confidence="high")
    s = run([a], {"a": model}, {"a": Overrides(fields={"waiting_on": ""})}).by_gid()["a"]
    assert s.bucket == "next"
    assert s.components["waiting_on"] is None
    assert s.components["waiting_source"] == "override"
    # a tag still beats the empty override
    t = facts("t", points=1, tags=("waiting:bank",))
    s = run([t], {"t": model}, {"t": Overrides(fields={"waiting_on": ""})}).by_gid()["t"]
    assert s.bucket == "nudge" and s.components["waiting_on"] == "bank"


def test_empty_waiting_tag_is_ignored():
    t = facts("t", points=1, tags=("waiting:",))
    s = run([t]).by_gid()["t"]
    assert s.bucket == "next" and s.components["waiting_source"] == "none"


def _model_wait(who="Michael"):
    return enr(waiting_on=who, waiting_confidence="high")


def test_model_wait_is_released_near_a_hard_deadline():
    # 5 points = 1.0 effort day; due in 6 days → raw slack 5.0 == N → released
    a = facts("a", points=5, due_on=TODAY + timedelta(days=6))
    s = run([a], {"a": _model_wait()}).by_gid()["a"]
    assert s.bucket == "next"
    assert s.components["waiting_on"] == "Michael"  # kept, for the waiting? flag
    assert s.components["wait_released"] == {"waiting_on": "Michael", "slack": 5.0}
    assert s.components["due_source"] == "hard"


def test_model_wait_holds_outside_the_slack_window():
    a = facts("a", points=5, due_on=TODAY + timedelta(days=7))  # raw slack 6.0 > N
    s = run([a], {"a": _model_wait()}).by_gid()["a"]
    assert s.bucket == "nudge" and s.components["wait_released"] is None


def test_override_wait_is_never_released():
    a = facts("a", points=5, due_on=TODAY)  # slack -1
    s = run([a], {"a": enr()}, {"a": Overrides(fields={"waiting_on": "FTB"})}).by_gid()["a"]
    assert s.bucket == "nudge" and s.components["wait_released"] is None
    t = facts("t", points=5, due_on=TODAY, tags=("waiting:FTB",))
    assert run([t]).by_gid()["t"].bucket == "nudge"


def test_soft_dates_never_release_a_wait():
    inferred = facts("i", points=1)
    e = enr(
        waiting_on="X",
        waiting_confidence="high",
        due_date_inferred=TODAY,
        due_date_inferred_confidence="high",
    )
    assert run([inferred], {"i": e}).by_gid()["i"].bucket == "nudge"
    horizon = facts("h", name="[P0] x", points=1, created_at=TS)  # P0 horizon long past
    assert run([horizon], {"h": _model_wait()}).by_gid()["h"].bucket == "nudge"


def test_inherited_hard_date_releases_a_childs_model_wait():
    parent = facts("p", points=1, due_on=TODAY + timedelta(days=2), num_open_subtasks=1)
    child = facts("c", points=1, parent_gid="p")
    s = run([parent, child], {"c": _model_wait()}).by_gid()["c"]
    assert s.bucket == "next"
    assert s.components["due_from"] == "p"
    assert s.components["wait_released"]["waiting_on"] == "Michael"


def test_pinned_waiting_task_is_not_double_handled_by_release():
    a = facts("a", points=5, due_on=TODAY)
    s = run([a], {"a": _model_wait()}, {"a": Overrides(pinned_rank=1)}).by_gid()["a"]
    assert s.bucket == "next"
    assert s.components["pinned_despite"] == "waiting"
    assert s.components["wait_released"] is None


def test_released_task_joins_feasibility_and_can_be_a_must_do():
    a = facts("a", points=5, due_on=TODAY + timedelta(days=3))  # slack 2
    scored = run([a], {"a": _model_wait()})
    s = scored.by_gid()["a"]
    assert s.components["effective_slack"] == 2.0 and s.components["simulated_start"] == 0.0
    assert [t.gid for t in pz.select(scored.next(), CFG, n=1)] == ["a"]


def test_must_do_is_decided_by_effective_slack():
    # 5 points = 1 effort day. EDF queue: near is first (due in 6 → slack 5 →
    # must-do); far is second (due in 8, minus its own day and near's → slack 6 → not).
    near = facts("n", name="[P3] file it", points=5, due_on=TODAY + timedelta(days=6))
    far = facts("f", name="[P3] file it", points=5, due_on=TODAY + timedelta(days=8))
    quick = facts("q", name="[P0] hot", points=1, due_on=TODAY + timedelta(days=20))
    scored = run([near, far, quick])
    picked = [t.gid for t in pz.select(scored.next(), CFG, n=1)]
    assert picked[0] == "n" and "f" not in picked


def test_edf_queue_makes_the_second_of_two_same_day_tasks_a_must_do_first():
    # both due in 7 days with 1 effort day: alone each has slack 6 (not a
    # must-do); queued, the second has slack 5 and is one.
    a = facts("a", name="[P1] x", points=5, due_on=TODAY + timedelta(days=7))
    b = facts("b", name="[P1] y", points=5, due_on=TODAY + timedelta(days=7))
    scored = run([a, b])
    slacks = sorted(t.components["effective_slack"] for t in scored.next())
    assert slacks == [5.0, 6.0]
    musts = [t.gid for t in pz.select(scored.next(), CFG, n=1)]
    assert len(musts) == 1 and scored.by_gid()[musts[0]].components["effective_slack"] == 5.0


def test_overdue_hard_task_is_a_must_do():
    late = facts("l", name="[P3] late", points=1, due_on=TODAY - timedelta(days=3))
    quick = facts("q", name="[P0] hot", points=1, due_on=TODAY + timedelta(days=20))
    assert pz.select(run([late, quick]).next(), CFG, n=1)[0].gid == "l"


def test_undated_task_is_never_a_must_do():
    from models.prioritize import ScoredTask

    t = ScoredTask(
        gid="u",
        bucket="next",
        score=1.0,
        position=1,
        rank=None,
        components={"due_source": "none", "effective_slack": None},
    )
    assert pz._is_must(t, CFG) is False

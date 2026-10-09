# tests/test_goal_state.py
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from models.strategy import Goal, GoalState, Measure, Signal, Strategy, Tripwire
from services import goal_state as gs
from services import prioritize_config as pc

CFG = pc.load()
TODAY = date(2026, 10, 9)


def view(gid, serves=("finances",), role="path", bucket="next", priority="P1", due_on=None,
         completed=False, completed_days_ago=None, tags=()):
    completed_at = (
        datetime(TODAY.year, TODAY.month, TODAY.day, tzinfo=timezone.utc) - timedelta(days=completed_days_ago)
        if completed_days_ago is not None else None
    )
    return gs.TaskView(gid, tuple(serves), role, bucket, priority, due_on, completed or completed_at is not None,
                       completed_at, tuple(tags))


def area(signals, gid="finances"):
    return Goal(id=gid, kind="area", signals=tuple(signals))


def outcome(**kw):
    base = dict(id="consulting", kind="outcome", horizon=date(2027, 3, 31),
                lag=Measure("revenue", ">=", 15000, "month"),
                leads=(Measure("conversation", ">=", 3, "week"),),
                tripwires=(Tripwire(1, "signed-client", "=", 0, date(2026, 12, 31), "Revisit niche"),
                           Tripwire(2, "lag", "<", 5000, date(2027, 1, 31), "Revisit pricing")))
    return Goal(**(base | kw))


def enough(n=5, **kw):
    return [view(f"f{i}", **kw) for i in range(n)]


def run(goals, views, reports=None, mutes=None, previous=None, today=TODAY, config=CFG):
    strat = Strategy(goals=tuple(goals), text_hash="h")
    return {s.goal_id: s for s in gs.evaluate(strat, views, reports or {}, mutes or {}, previous or {}, today, config)}


OVERDUE = Signal(kind="overdue", cls="evidence", text="overdue")


def test_overdue_default_requires_p1_path_actionable_past_grace():
    late = TODAY - timedelta(days=5)
    cases = {
        "counts": view("a", due_on=late),
        "support_role": view("b", role="support", due_on=late),
        "p2": view("c", priority="P2", due_on=late),
        "snoozed": view("d", bucket="snoozed", due_on=late),
        "waiting": view("e", bucket="nudge", due_on=late),
        "inside_grace": view("f", due_on=TODAY - timedelta(days=2)),
        "no_date": view("g"),
    }
    for name, v in cases.items():
        s = run([area([OVERDUE])], enough() + [v])["finances"]
        sig = s.state["signals"][0]
        assert sig["raw"] is (name == "counts"), name
        assert sig["tasks"] == (["a"] if name == "counts" else []), name


def test_overdue_widened_by_priority_and_grace():
    wide = Signal(kind="overdue", cls="evidence", text="overdue:P2+ grace 0", min_priority="P2", grace=0)
    s = run([area([wide])], enough() + [view("c", priority="P2", due_on=TODAY - timedelta(days=1))])
    assert s["finances"].state["signals"][0]["raw"] is True


def test_overdue_tag_variant_ignores_priority():
    tagged = Signal(kind="overdue", cls="evidence", text="overdue:bill", min_priority="P3", tag="bill")
    s = run([area([tagged])], enough() + [view("c", priority="P3", due_on=TODAY - timedelta(days=9), tags=("bill",))])
    assert s["finances"].state["signals"][0]["raw"] is True


def test_undated_after_date():
    sig = Signal(kind="undated", cls="evidence", text="undated:tax after 2026-10-01", tag="tax", after=date(2026, 10, 1))
    s = run([area([sig])], enough() + [view("t", tags=("tax",))])
    assert s["finances"].state["signals"][0]["raw"] is True
    s = run([area([sig])], enough() + [view("t", tags=("tax",))], today=date(2026, 9, 30))
    assert s["finances"].state["signals"][0]["raw"] is False


def test_stale_is_no_data_without_open_actionable_tasks():
    sig = Signal(kind="stale", cls="absence", text="stale > 14 days", days=14)
    done = [view(f"d{i}", completed_days_ago=30) for i in range(5)]
    assert run([area([sig])], done)["finances"].state["signals"][0]["raw"] is None
    assert run([area([sig])], done + [view("open")])["finances"].state["signals"][0]["raw"] is True
    recent = done + [view("open"), view("r", completed_days_ago=3)]
    assert run([area([sig])], recent)["finances"].state["signals"][0]["raw"] is False


def test_lead_signal_is_no_data_without_tag_history():
    sig = Signal(kind="lead", cls="absence", text="lead bill < 2 per month", tag="bill", value=2, period="month")
    assert run([area([sig])], enough())["finances"].state["signals"][0]["raw"] is None
    old = enough() + [view("x", completed_days_ago=80, tags=("bill",))]
    assert run([area([sig])], old)["finances"].state["signals"][0]["raw"] is True
    fresh = old + [view("y", completed_days_ago=2, tags=("bill",)), view("z", completed_days_ago=5, tags=("bill",))]
    assert run([area([sig])], fresh)["finances"].state["signals"][0]["raw"] is False


def test_rollout_guard_reports_no_data_under_min_tagged():
    s = run([area([OVERDUE])], [view("a", due_on=TODAY - timedelta(days=9))])
    assert s["finances"].state["signals"][0]["raw"] is None and s["finances"].state["below_the_line"] is False


def _prev(raw_days, effective=False):
    return {"finances": GoalState("finances", "area", TODAY - timedelta(days=1), "h", {
        "signals": [{"signal": "overdue", "raw": True, "effective": effective, "consecutive_days": raw_days}]})}


def test_debounce_flips_after_three_consecutive_days_and_clears_after_three():
    late = enough() + [view("a", due_on=TODAY - timedelta(days=9))]
    assert run([area([OVERDUE])], late)["finances"].state["signals"][0]["effective"] is False
    s = run([area([OVERDUE])], late, previous=_prev(2))["finances"].state["signals"][0]
    assert s["effective"] is True and s["consecutive_days"] == 3
    # now clear: raw false today, effective was true
    prev_true = {"finances": GoalState("finances", "area", TODAY - timedelta(days=1), "h", {
        "signals": [{"signal": "overdue", "raw": False, "effective": True, "consecutive_days": 2}]})}
    s = run([area([OVERDUE])], enough(), previous=prev_true)["finances"].state["signals"][0]
    assert s["effective"] is False and s["consecutive_days"] == 3
    prev_true["finances"].state["signals"][0]["consecutive_days"] = 1
    s = run([area([OVERDUE])], enough(), previous=prev_true)["finances"].state["signals"][0]
    assert s["effective"] is True and s["consecutive_days"] == 2


def test_mute_hides_below_the_line_but_keeps_raw():
    late = enough() + [view("a", due_on=TODAY - timedelta(days=9))]
    s = run([area([OVERDUE])], late, mutes={"finances": TODAY + timedelta(days=3)}, previous=_prev(2))["finances"]
    assert s.state["signals"][0]["raw"] is True and s.state["signals"][0]["effective"] is True
    assert s.state["below_the_line"] is False and s.state["muted_until"] == (TODAY + timedelta(days=3)).isoformat()


def test_lead_rates_and_lag():
    views = [view(f"c{i}", serves=("consulting",), completed_days_ago=i, tags=("conversation",)) for i in (1, 2, 3, 9)]
    reports = {"consulting": [{"value": 4000.0, "period_start": date(2026, 9, 1), "reported_at": None}]}
    s = run([outcome()], views, reports=reports)["consulting"].state
    lead = s["leads"][0]
    assert (lead["tag"], lead["window"], lead["value"], lead["threshold"], lead["met"]) == ("conversation", "week", 3, 3.0, True)
    assert s["lag"] == {"value": 4000.0, "threshold": 15000.0, "met": False, "period_start": "2026-09-01"}


def test_tripwire_fires_only_from_its_by_date():
    g = outcome()
    s = run([g], [], today=date(2026, 12, 30))["consulting"].state["tripwires"]
    assert s[0]["fired"] is False and s[0]["evaluated"] is False
    s = run([g], [], today=date(2026, 12, 31))["consulting"].state["tripwires"]
    assert s[0]["fired"] is True and s[0]["value"] == 0
    signed = [view("w", serves=("consulting",), completed_days_ago=10, tags=("signed-client",))]
    s = run([g], signed, today=date(2026, 12, 31))["consulting"].state["tripwires"]
    assert s[0]["fired"] is False


def test_lag_tripwire_uses_latest_report_and_cannot_fire_without_one():
    g = outcome()
    s = run([g], [], today=date(2027, 2, 1))["consulting"].state["tripwires"][1]
    assert s["fired"] is False and s["value"] is None
    s = run([g], [], reports={"consulting": [{"value": 1000.0, "period_start": None, "reported_at": None}]},
            today=date(2027, 2, 1))["consulting"].state["tripwires"][1]
    assert s["fired"] is True


def test_diagnosis_rule():
    strong = [view(f"c{i}", serves=("consulting",), completed_days_ago=i, tags=("conversation",)) for i in (1, 2, 3)]
    flat = {"consulting": [{"value": 100.0, "period_start": None, "reported_at": None}] * 2}
    assert run([outcome()], strong, reports=flat)["consulting"].state["diagnosis"] == "lead strong, lag flat"
    one = {"consulting": [{"value": 100.0, "period_start": None, "reported_at": None}]}
    assert run([outcome()], strong, reports=one)["consulting"].state["diagnosis"] == "insufficient data"
    assert run([outcome()], [], reports=flat)["consulting"].state["diagnosis"] == "lead weak"
    met = {"consulting": [{"value": 20000.0, "period_start": None, "reported_at": None}] * 2}
    assert run([outcome()], strong, reports=met)["consulting"].state["diagnosis"] == "on track"


def test_views_from_reads_components_and_facts():
    from tests.test_prioritize import facts
    f = facts("a", due_on=TODAY, tags=("bill",))
    scores = {"a": {"bucket": "next", "components": {"serves": ["finances"], "role": "derisk"}}}
    v = gs.views_from([f], scores)[0]
    assert (v.serves, v.role, v.bucket, v.priority, v.due_on, v.tags) == (("finances",), "derisk", "next", "P1", TODAY, ("bill",))
    assert gs.views_from([f], {})[0].bucket == "next"

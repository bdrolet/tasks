# tests/test_strategy.py
from datetime import date

from models.strategy import Strategy
from services import standing_context
from services import strategy as st

TODAY = date(2026, 10, 9)
DOC = """- last reviewed: 2026-10-01

### consulting
- kind: outcome
- weight: 1.0
- horizon: 2027-03-31
- lag: consulting revenue >= 15000 per month
- lead: conversation >= 3 per week
- lead: proposal >= 2 per month
- tripwire: signed-client = 0 by 2026-12-31 -> Revisit consulting niche and offer
- tripwire: lag < 5000 by 2027-01-31 -> Revisit pricing

**Diagnosis.** Nobody knows I exist.
**Guiding policy.** Referrals only. Not doing: cold email.

### finances
- kind: area
- weight: 0.9
- standard: bills paid on time
- below-the-line: overdue; undated:tax after 2026-10-31; stale > 35 days; lead bill < 1 per month; overdue:P2+ grace 7

**Standard.** Enough money.
"""


def test_parse_outcome_goal_header_and_prose():
    s = st.parse(DOC, today=TODAY)
    assert s.last_reviewed == date(2026, 10, 1)
    g = s.get("consulting")
    assert g.kind == "outcome" and g.weight == 1.0 and g.horizon == date(2027, 3, 31)
    assert g.lag.tag == "consulting revenue" and g.lag.op == ">=" and g.lag.value == 15000
    assert g.lag.period == "month"
    assert [(m.tag, m.op, m.value, m.period) for m in g.leads] == [
        ("conversation", ">=", 3.0, "week"),
        ("proposal", ">=", 2.0, "month"),
    ]
    t1, t2 = g.tripwires
    assert (t1.ordinal, t1.subject, t1.op, t1.value, t1.by) == (
        1,
        "signed-client",
        "=",
        0.0,
        date(2026, 12, 31),
    )
    assert t1.action == "Revisit consulting niche and offer"
    assert t2.subject == "lag" and t2.action == "Revisit pricing"
    assert "**Diagnosis.** Nobody knows I exist." in g.prose
    assert "- kind:" not in g.prose


def test_parse_area_signals():
    g = st.parse(DOC, today=TODAY).get("finances")
    assert g.kind == "area" and g.standard == "bills paid on time"
    kinds = [(x.kind, x.cls) for x in g.signals]
    assert kinds == [
        ("overdue", "evidence"),
        ("undated", "evidence"),
        ("stale", "absence"),
        ("lead", "absence"),
        ("overdue", "evidence"),
    ]
    overdue_default, undated, stale, lead, overdue_wide = g.signals
    assert overdue_default.min_priority == "P1" and overdue_default.grace == 3
    assert undated.tag == "tax" and undated.after == date(2026, 10, 31)
    assert stale.days == 35
    assert (lead.tag, lead.value, lead.period) == ("bill", 1.0, "month")
    assert overdue_wide.min_priority == "P2" and overdue_wide.grace == 7


def test_malformed_block_is_skipped_with_a_finding():
    doc = DOC + "\n### broken\n- weight: 2\n\nprose\n\n### alsobroken\n- kind: sideways\n"
    s = st.parse(doc, today=TODAY)
    assert {g.id for g in s.goals} == {"consulting", "finances"}
    assert any("broken: missing kind" in f for f in s.findings)
    assert any("alsobroken: unknown kind" in f for f in s.findings)


def test_bad_header_line_is_a_finding_not_a_failure():
    doc = DOC.replace("- lead: proposal >= 2 per month", "- lead: proposal twice weekly")
    s = st.parse(doc, today=TODAY)
    assert s.get("consulting") is not None
    assert len(s.get("consulting").leads) == 1
    assert any("consulting: lead" in f for f in s.findings)


def test_duplicate_id_keeps_first():
    doc = DOC + "\n### consulting\n- kind: area\n- standard: x\n"
    s = st.parse(doc, today=TODAY)
    assert s.get("consulting").kind == "outcome"
    assert any("duplicate id consulting" in f for f in s.findings)


def test_empty_text_is_empty_strategy():
    assert st.parse("", today=TODAY) == Strategy.EMPTY
    assert st.parse("   \n", today=TODAY).goals == ()


def test_review_findings_for_missing_tripwire_lead_and_stale_doc():
    doc = "- last reviewed: 2026-01-01\n\n### biz\n- kind: outcome\n- horizon: 2027-01-01\n"
    s = st.parse(doc, today=TODAY, stale_after_days=90)
    assert "biz: outcome goal has no tripwire" in s.findings
    assert "biz: outcome goal has no lead measure" in s.findings
    assert any(f.startswith("strategy last reviewed 281 days ago") for f in s.findings)


def test_unknown_signal_is_ignored_with_a_finding():
    doc = "### home\n- kind: area\n- below-the-line: dishes > 3; stale > 14 days\n"
    s = st.parse(doc, today=TODAY)
    assert [x.kind for x in s.get("home").signals] == ["stale"]
    assert any("home: below-the-line" in f for f in s.findings)


def test_invalid_date_drops_only_that_signal():
    doc = (
        "### home\n- kind: area\n- below-the-line: undated:tax after 2026-02-30; stale > 14 days\n"
    )
    s = st.parse(doc, today=TODAY)
    assert [x.kind for x in s.get("home").signals] == ["stale"]
    assert any(
        "home: below-the-line signal 'undated:tax after 2026-02-30' has an invalid date" in f
        for f in s.findings
    )


def test_text_hash_is_stable_and_changes_with_text():
    assert st.text_hash(DOC) == st.text_hash(DOC)
    assert st.text_hash(DOC) != st.text_hash(DOC + "x")
    assert st.parse(DOC, today=TODAY).text_hash == st.text_hash(DOC)


def test_load_reads_the_strategy_section(monkeypatch):
    monkeypatch.setattr(
        standing_context, "section", lambda name, **kw: DOC if name == "Strategy" else ""
    )
    s = st.load(today=TODAY)
    assert {g.id for g in s.goals} == {"consulting", "finances"}


def test_load_without_section_is_empty(monkeypatch):
    monkeypatch.setattr(standing_context, "section", lambda name, **kw: "")
    assert st.load(today=TODAY) == Strategy.EMPTY

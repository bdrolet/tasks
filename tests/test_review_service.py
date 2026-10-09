# tests/test_review_service.py
from datetime import date

from models.strategy import Goal, GoalState, Strategy
from services import review

TODAY = date(2026, 10, 9)
STRAT = Strategy(
    goals=(Goal(id="consulting", kind="outcome"), Goal(id="finances", kind="area")),
    last_reviewed=date(2026, 9, 1),
    findings=("consulting: outcome goal has no tripwire",),
    text_hash="h",
)


def score(gid, name, bucket="next", **components):
    return {
        "task_gid": gid,
        "name": name,
        "bucket": bucket,
        "position": 1,
        "rank": None,
        "score": 1.0,
        "permalink_url": f"u/{gid}",
        "project_name": "Work",
        "components": {
            "serves": [],
            "role": None,
            "grooming": False,
            "confident_none": False,
            "reason": "r",
            "necessity_confidence": "low",
        }
        | components,
    }


def test_build_shapes_goals_grooming_and_stop_doing():
    states = {
        "consulting": GoalState(
            "consulting",
            "outcome",
            TODAY,
            "h",
            {
                "next_step": "t1",
                "stalled": False,
                "leads": [
                    {
                        "tag": "conversation",
                        "window": "week",
                        "value": 1,
                        "threshold": 3,
                        "met": False,
                    }
                ],
                "lag": None,
                "tripwires": [],
                "diagnosis": "insufficient data",
            },
        ),
        "finances": GoalState(
            "finances",
            "area",
            TODAY,
            "h",
            {
                "below_the_line": True,
                "muted_until": None,
                "next_step": "t2",
                "signals": [
                    {
                        "signal": "overdue",
                        "class": "evidence",
                        "raw": True,
                        "effective": True,
                        "consecutive_days": 4,
                        "state": "true",
                        "tasks": ["t2"],
                    }
                ],
            },
        ),
    }
    scores = [
        score("t1", "[P1] Write offer", serves=["consulting"], role="path"),
        score("t2", "[P1] Pay bill", serves=["finances"], role="derisk"),
        score(
            "t3",
            "[P2] Maybe",
            grooming=True,
            necessity_confidence="low",
            serves_suggested=["consulting"],
        ),
        score("t4", "[P3] Fluff", confident_none=True, necessity_confidence="high"),
        score(
            "t5", "[P3] Gone", bucket="stop_doing", confident_none=True, necessity_confidence="high"
        ),
    ]
    sup = [
        {
            "message_id": "m",
            "subject": "Newsletter",
            "sender": "x",
            "reason": "r",
            "web_link": "w",
            "created_at": None,
            "restored_at": None,
        }
    ]
    r = review.build(STRAT, states, scores, sup, TODAY)
    assert r["strategy_last_reviewed"] == "2026-09-01" and r["findings"] == [
        "consulting: outcome goal has no tripwire"
    ]
    goals = {g["id"]: g for g in r["goals"]}
    assert goals["consulting"]["next_step"] == {
        "gid": "t1",
        "name": "[P1] Write offer",
        "permalink_url": "u/t1",
    }
    assert (
        goals["consulting"]["stalled"] is False
        and goals["consulting"]["diagnosis"] == "insufficient data"
    )
    assert goals["finances"]["below_the_line"] is True
    assert goals["finances"]["next_step"]["gid"] == "t2" and goals["finances"]["stalled"] is False
    assert goals["finances"]["signals"][0]["tasks"] == [
        {"gid": "t2", "name": "[P1] Pay bill", "permalink_url": "u/t2"}
    ]
    assert [g["gid"] for g in r["grooming"]] == ["t3"]
    assert [g["gid"] for g in r["stop_doing"]["tasks"]] == ["t4", "t5"]
    assert r["stop_doing"]["suppressed_emails"][0]["subject"] == "Newsletter"


def test_render_is_markdown_with_links():
    states = {
        "consulting": GoalState(
            "consulting",
            "outcome",
            TODAY,
            "h",
            {
                "next_step": "t1",
                "stalled": False,
                "leads": [
                    {
                        "tag": "conversation",
                        "window": "week",
                        "value": 1,
                        "threshold": 3,
                        "met": False,
                    },
                    {"tag": "proposal", "window": "month", "value": 2, "threshold": 2, "met": True},
                ],
                "lag": {"value": 500, "threshold": 4000, "met": False},
                "tripwires": [
                    {"text": "no calls", "action": "pivot", "fired": True, "evaluated": True}
                ],
                "diagnosis": "execution",
            },
        ),
        "finances": GoalState(
            "finances",
            "area",
            TODAY,
            "h",
            {
                "below_the_line": True,
                "muted_until": None,
                "signals": [
                    {
                        "signal": "overdue",
                        "class": "evidence",
                        "state": "true",
                        "consecutive_days": 4,
                        "tasks": ["t2"],
                    }
                ],
            },
        ),
    }
    scores = [
        score("t1", "[P1] Write offer", serves=["consulting"], role="path"),
        score("t2", "[P1] Pay bill", serves=["finances"], role="derisk"),
        score("t3", "[P2] Maybe", grooming=True, serves_suggested=["consulting"]),
        score("t4", "[P3] Fluff", confident_none=True, necessity_confidence="high"),
    ]
    sup = [
        {
            "message_id": "m1",
            "subject": "Linked",
            "sender": "a@x",
            "reason": "r1",
            "web_link": "https://o/m1",
            "created_at": None,
            "restored_at": None,
        },
        {
            "message_id": "m2",
            "subject": "Unlinked",
            "sender": "b@x",
            "reason": "r2",
            "web_link": None,
            "created_at": None,
            "restored_at": None,
        },
    ]
    md = review.render(review.build(STRAT, states, scores, sup, TODAY))
    assert md.startswith("# Weekly strategy review — 2026-10-09")
    assert "## consulting" in md and "outcome goal has no tripwire" in md
    assert "- next step: [[P1] Write offer](u/t1)" in md
    assert "- lead conversation: 1 / 3 per week — not met" in md
    assert "- lead proposal: 2 / 2 per month — met" in md
    assert "- lag: 500 / 4000 — not met" in md
    assert "- tripwire no calls → pivot: **FIRED**" in md
    assert "  - overdue [evidence]: true (4d) — [[P1] Pay bill](u/t2)" in md
    assert "- [[P2] Maybe](u/t3) — suggested consulting" in md
    assert "- [[P3] Fluff](u/t4) —" in md
    assert "- email [Linked](https://o/m1) from a@x — r1" in md
    assert "- email Unlinked from b@x — r2" in md
    assert "None" not in md


def test_necessity_calibration_groups_by_band_and_source():
    rows = [
        {
            "task_gid": "a",
            "serves_estimated": {"serves": ["consulting"], "role": "path", "confidence": "high"},
            "tags": ["serves:consulting", "role:path"],
            "overrides": None,
            "source": "enrichment",
        },
        {
            "task_gid": "b",
            "serves_estimated": {"serves": ["consulting"], "role": "path", "confidence": "high"},
            "tags": ["serves:finances", "role:path"],
            "overrides": None,
            "source": "enrichment",
        },
        {
            "task_gid": "c",
            "serves_estimated": {"grooming": True},
            "tags": ["serves:finances"],
            "overrides": None,
            "source": "enrichment",
        },
        {
            "task_gid": "d",
            "serves_estimated": {"grooming": True},
            "tags": [],
            "overrides": None,
            "source": "enrichment",
        },
        {
            "task_gid": "e",
            "serves_estimated": {"serves": ["finances"], "role": "support", "confidence": "medium"},
            "tags": ["serves:finances", "role:support"],
            "overrides": None,
            "source": "gate2",
        },
    ]
    rates = [{"band": "high", "restored": 1, "settled": 3, "pending": 2}]
    c = review.necessity_calibration(rows, rates)
    assert c["by_confidence"]["high"] == {"judged": 2, "agreed": 1, "rate": 0.5}
    assert c["by_source"]["gate2"] == {"judged": 1, "agreed": 1, "rate": 1.0}
    assert c["grooming"] == {"attached": 1, "unresolved": 1}
    assert c["suppressions"]["high"] == {
        "restored": 1,
        "settled": 3,
        "pending": 2,
        "restore_rate": 0.25,
    }


def test_gate2_vs_enrichment_agreement():
    def row(gid, source, est):
        return {
            "task_gid": gid,
            "serves_estimated": est,
            "tags": [],
            "overrides": None,
            "source": source,
        }

    path = {"serves": ["a", "b"], "role": "path", "confidence": "high"}
    rows = [
        row("agree", "gate2", path),
        row("agree", "enrichment", path | {"serves": ["b", "a"]}),
        row("differ", "gate2", path),
        row("differ", "enrichment", {"serves": ["a"], "role": "path", "confidence": "high"}),
        row("groom", "gate2", {"grooming": True}),
        row("groom", "enrichment", path),
        row("solo", "gate2", path),
    ]
    c = review.necessity_calibration(rows, [])
    assert c["gate2_vs_enrichment"] == {"judged": 2, "agreed": 1, "rate": 0.5}
    assert review.necessity_calibration([], [])["gate2_vs_enrichment"] == {
        "judged": 0,
        "agreed": 0,
        "rate": None,
    }

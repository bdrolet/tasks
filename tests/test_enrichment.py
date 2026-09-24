import json
from datetime import date

import pytest

from services import enrichment as en

COMMENTS = [
    {
        "text": "sent to the lawyer, waiting on redlines",
        "created_by": "Ben",
        "created_at": "2026-09-20T10:00:00Z",
    },
]
GOOD = {
    "story_points_suggested": 3,
    "points_confidence": "medium",
    "waiting_on": "the lawyer",
    "due_date_inferred": "2026-09-30",
    "due_date_inferred_confidence": "high",
    "impact": "high",
    "energy": "deep",
    "latest_comment_signal": "none",
    "reason": "redlines outstanding",
}


def test_hash_is_stable_across_estimate_comment_and_changes_on_others():
    base = en.content_hash("n", "notes", COMMENTS)
    with_estimate = en.content_hash(
        "n",
        "notes",
        COMMENTS
        + [
            {
                "text": "Estimated 3 points — adjust if wrong.",
                "created_by": "tasks",
                "created_at": "x",
            }
        ],
    )
    with_other = en.content_hash(
        "n", "notes", COMMENTS + [{"text": "ping", "created_by": "Ben", "created_at": "y"}]
    )
    assert base == with_estimate
    assert base != with_other
    assert base != en.content_hash("n2", "notes", COMMENTS)


def test_hash_tolerates_null_comment_text():
    assert en.content_hash("n", "", [{"text": None, "created_by": None, "created_at": None}])


def test_user_prompt_labels_comments_and_dates():
    p = en.user_prompt(
        name="[P1] Reply",
        project="Inbox",
        notes_text="body",
        comments=COMMENTS,
        due_on=date(2026, 10, 1),
        start_on=None,
        tags=["cheryl"],
        today=date(2026, 9, 23),
    )
    assert "Today: 2026-09-23" in p and "Due: 2026-10-01" in p and "Tags: cheryl" in p
    assert "[2026-09-20] Ben: sent to the lawyer" in p


def test_parse_validates_and_flags_enriched():
    e = en.parse(json.dumps(GOOD))
    assert e.story_points_suggested == 3 and e.due_date_inferred == date(2026, 9, 30)
    assert e.unenriched is False and e.reason == "redlines outstanding"


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "story_points_suggested": 4},
        {**GOOD, "impact": "huge"},
        {**GOOD, "due_date_inferred": "soon"},
        "not json",
    ],
)
def test_parse_rejects_schema_violations(bad):
    raw = bad if isinstance(bad, str) else json.dumps(bad)
    with pytest.raises(ValueError):
        en.parse(raw)


def test_extract_uses_injected_call_and_model():
    seen = {}

    def fake(*, model, system, user, schema, effort="low", max_tokens=8000):
        seen.update(model=model, effort=effort, schema=schema)
        return json.dumps(GOOD)

    e = en.extract(
        name="[P1] Reply",
        project="Inbox",
        html_notes="<body>body</body>",
        comments=COMMENTS,
        due_on=None,
        start_on=None,
        tags=[],
        today=date(2026, 9, 23),
        call=fake,
    )
    assert e.impact == "high"
    assert seen["model"] == "claude-opus-5" and seen["effort"] == "low"
    assert seen["schema"]["required"] == list(en.SCHEMA["properties"])


def test_is_estimate_comment():
    assert en.is_estimate_comment("Estimated 3 points — adjust if wrong.")
    assert not en.is_estimate_comment("estimated delivery is tuesday")
    assert not en.is_estimate_comment(None)

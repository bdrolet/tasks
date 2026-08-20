import pytest

from services import policy
from tests.test_events import make_email_event


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("urgent", True),
        ("review", True),
        ("respond", True),
        ("reference", False),
        ("ignore", False),
    ],
)
def test_warrants_task(category, expected):
    assert policy.warrants_task(make_email_event(category=category)) is expected


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        (
            ["Final payment will be sent; no action required unless you cancel"],
            "Final payment will be sent; no action required unless you cancel",
        ),
        (["No action is needed"], "No action is needed"),
        (["Statement attached for your records"], "Statement attached for your records"),
        (
            ["Your automatic payment of $70 will draw on 9/9"],
            "Your automatic payment of $70 will draw on 9/9",
        ),
        (["Enrolled in autopay"], "Enrolled in autopay"),
        (["Pay the $70 bill by 9/9"], None),
        ([], None),
    ],
)
def test_no_action_phrase(points, expected):
    assert policy.no_action_phrase(points) == expected


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        # Routine, successful autopay/automatic-payment mentions still veto.
        (
            ["Your automatic payment was processed successfully"],
            "Your automatic payment was processed successfully",
        ),
        (["Enrolled in autopay"], "Enrolled in autopay"),
        # A failure/attention-needed signal on the SAME key point must fail
        # open — an unconditional veto here would swallow a real task.
        (["Your automatic payment failed — update your card"], None),
        (["Autopay could not be processed; action required"], None),
        # Scanning continues past a disqualified conditional match to find
        # a later, genuine unconditional no-action point.
        (
            [
                "Your automatic payment failed — update your card",
                "No action required for this month",
            ],
            "No action required for this month",
        ),
    ],
)
def test_no_action_phrase_conditional_autopay_veto(points, expected):
    assert policy.no_action_phrase(points) == expected


@pytest.mark.parametrize(
    ("points", "expected"),
    [
        # "for your records" attached to a real ask (sign/return) must not
        # swallow the task — the disqualifier now covers obligation language.
        (
            ["Sign and return the form by Friday; keep a copy for your records"],
            None,
        ),
        # No obligation language present — the conditional veto still fires.
        (["Statement attached for your records"], "Statement attached for your records"),
        (["Please RSVP; details are for your records"], None),
        # The unconditional "no action required" phrase still vetoes even
        # when the same point also carries disqualifier words that would
        # block a conditional match.
        (
            [
                "No action required — your payment failed to process automatically "
                "last month but has since been corrected"
            ],
            "No action required — your payment failed to process automatically "
            "last month but has since been corrected",
        ),
    ],
)
def test_no_action_phrase_for_your_records_conditional(points, expected):
    assert policy.no_action_phrase(points) == expected

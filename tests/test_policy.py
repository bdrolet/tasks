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
        (["Final payment will be sent; no action required unless you cancel"], "no action required"),
        (["No action is needed"], "no action is needed"),
        (["Statement attached for your records"], "for your records"),
        (["Your automatic payment of $70 will draw on 9/9"], "automatic payment"),
        (["Enrolled in autopay"], "autopay"),
        (["Pay the $70 bill by 9/9"], None),
        ([], None),
    ],
)
def test_no_action_phrase(points, expected):
    assert policy.no_action_phrase(points) == expected

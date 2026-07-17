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

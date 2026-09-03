from datetime import date

import pytest
from dateutil.relativedelta import relativedelta

from services import recurrence


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("repeat:10d", relativedelta(days=10)),
        ("repeat:1day", relativedelta(days=1)),
        ("repeat:3days", relativedelta(days=3)),
        ("repeat:2w", relativedelta(weeks=2)),
        ("repeat:2weeks", relativedelta(weeks=2)),
        ("repeat:3mo", relativedelta(months=3)),
        ("repeat:3mon", relativedelta(months=3)),
        ("repeat:1month", relativedelta(months=1)),
        ("repeat:6months", relativedelta(months=6)),
        ("repeat:1y", relativedelta(years=1)),
        ("repeat:2yr", relativedelta(years=2)),
        ("repeat:1year", relativedelta(years=1)),
        ("REPEAT:3MO", relativedelta(months=3)),
        ("repeat: 3 mo ", relativedelta(months=3)),
    ],
)
def test_parse_accepts_every_unit_and_alias(tag, expected):
    assert recurrence.parse(tag) == expected


@pytest.mark.parametrize(
    "tag",
    [
        "repeat:3m",  # bare m — ambiguous between minutes and months
        "repeat:0d",
        "repeat:-3mo",
        "repeat:3651d",
        "repeat:mo",
        "repeat:3",
        "repeat:three months",
        "repeat:",
        "repeat:3fortnights",
        "urgent",  # not a repeat tag at all
        "",
    ],
)
def test_parse_rejects_malformed_rules(tag):
    assert recurrence.parse(tag) is None


def test_find_rule_returns_gid_and_interval():
    tags = [{"gid": "t1", "name": "home"}, {"gid": "t2", "name": "repeat:3mo"}]
    assert recurrence.find_rule(tags) == ("t2", relativedelta(months=3))


def test_find_rule_without_a_repeat_tag_is_none():
    assert recurrence.find_rule([{"gid": "t1", "name": "home"}]) is None
    assert recurrence.find_rule([]) is None


def test_two_repeat_tags_are_ambiguous_and_ignored(caplog):
    tags = [{"gid": "t1", "name": "repeat:3mo"}, {"gid": "t2", "name": "repeat:1y"}]
    assert recurrence.find_rule(tags) is None
    assert "repeat tags" in caplog.text


def test_unparseable_repeat_tag_is_ignored():
    assert recurrence.find_rule([{"gid": "t1", "name": "repeat:3months?"}]) is None


def test_next_due_adds_days():
    assert recurrence.next_due("2026-09-03T14:00:00.000Z", relativedelta(days=10)) == date(
        2026, 9, 13
    )


def test_next_due_clamps_to_end_of_month():
    # Jan 31 + 1 month has no Feb 31 to land on; relativedelta clamps.
    assert recurrence.next_due("2026-01-31T14:00:00.000Z", relativedelta(months=1)) == date(
        2026, 2, 28
    )


def test_next_due_handles_leap_year():
    assert recurrence.next_due("2024-01-31T14:00:00.000Z", relativedelta(months=1)) == date(
        2024, 2, 29
    )


def test_next_due_uses_the_local_completion_date_not_utc():
    # 23:30 UTC on the 3rd is 19:30 ET on the 3rd. Taking the UTC date would
    # date the successor a day late.
    assert recurrence.next_due("2026-09-03T23:30:00.000Z", relativedelta(days=1)) == date(
        2026, 9, 4
    )


def test_next_due_treats_a_naive_timestamp_as_utc():
    assert recurrence.next_due("2026-09-03T23:30:00", relativedelta(days=1)) == date(2026, 9, 4)


def test_next_due_falls_back_to_today_without_a_timestamp():
    assert recurrence.next_due(None, relativedelta(days=1)) == date.today() + relativedelta(days=1)

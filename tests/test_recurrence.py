from datetime import date

import pytest
from dateutil.relativedelta import relativedelta

import clients.asana as asana
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
    # 23:30 UTC on the 3rd is 19:30 ET on the 3rd — same calendar day.
    # This tests the ordinary (non-boundary) path.
    assert recurrence.next_due("2026-09-03T23:30:00.000Z", relativedelta(days=1)) == date(
        2026, 9, 4
    )


def test_next_due_crosses_utc_et_date_boundary():
    # 02:00 UTC on the 4th is 22:00 ET on the 3rd — different calendar days.
    # This boundary case proves the conversion to local TZ is actually used:
    # without it, an implementation taking the UTC date would wrongly return 2026-09-05.
    assert recurrence.next_due("2026-09-04T02:00:00.000Z", relativedelta(days=1)) == date(
        2026, 9, 4
    )


def test_next_due_treats_a_naive_timestamp_as_utc():
    assert recurrence.next_due("2026-09-03T23:30:00", relativedelta(days=1)) == date(2026, 9, 4)


def test_next_due_falls_back_to_today_without_a_timestamp():
    assert recurrence.next_due(None, relativedelta(days=1)) == date.today() + relativedelta(days=1)


class FakeCreated:
    def __init__(self, gid: str, permalink_url: str):
        self.gid = gid
        self.permalink_url = permalink_url


@pytest.fixture
def asana_stub(monkeypatch):
    """Records every Asana write the successor path makes."""
    calls: dict = {
        "created": [],
        "sections": [],
        "removed_tags": [],
        "stories": [],
        "refreshed": [],
    }
    monkeypatch.setattr(asana, "ASANA_PROJECT_ID", "proj-1")
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: None)
    monkeypatch.setattr(
        asana,
        "create_task_from_fields",
        lambda fields: (
            calls["created"].append(fields),
            FakeCreated("new-1", "https://app.asana.com/0/1/new-1"),
        )[1],
    )
    monkeypatch.setattr(asana, "add_task_to_section", lambda t, s: calls["sections"].append((t, s)))
    monkeypatch.setattr(asana, "remove_tag", lambda t, g: calls["removed_tags"].append((t, g)))
    monkeypatch.setattr(
        asana, "create_story", lambda gid, **kw: calls["stories"].append((gid, kw)) or {}
    )
    monkeypatch.setattr(
        recurrence.task_index, "refresh", lambda gid: calls["refreshed"].append(gid)
    )
    return calls


def _task(**over):
    base = {
        "gid": "old-1",
        "name": "[P2] Change the furnace filter",
        "completed": True,
        "completed_at": "2026-09-03T14:00:00.000Z",
    }
    return {**base, **over}


def _detail(**over):
    base = {
        "name": "[P2] Change the furnace filter",
        "html_notes": "<body>Filter size 16x25x1</body>",
        "tags": [{"gid": "t-home", "name": "home"}, {"gid": "t2", "name": "repeat:3mo"}],
        "assignee": {"gid": "u-1", "name": "Ben"},
    }
    return {**base, **over}


def test_spawn_next_creates_a_dated_copy(asana_stub, monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    new_gid = recurrence.spawn_next(
        _task(), _detail(), {"gid": "sec-review", "name": "Review"}, ("t2", relativedelta(months=3))
    )

    assert new_gid == "new-1"
    fields = asana_stub["created"][0]
    assert fields["name"] == "[P2] Change the furnace filter"
    assert fields["html_notes"] == "<body>Filter size 16x25x1</body>"
    assert fields["due_on"] == "2026-12-03"
    assert fields["external"] == {"gid": "recur:old-1"}
    assert fields["projects"] == ["proj-1"]
    assert fields["assignee"] == "u-1"
    # The repeat tag rides along, so the successor inherits the rule.
    assert set(fields["tags"]) == {"t-home", "t2"}


def test_spawn_next_places_the_successor_in_the_previous_section(asana_stub, monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    recurrence.spawn_next(
        _task(), _detail(), {"gid": "sec-review", "name": "Review"}, ("t2", relativedelta(months=3))
    )
    assert asana_stub["sections"] == [("new-1", "sec-review")]


def test_spawn_next_leaves_the_successor_unsectioned_when_the_old_one_was_in_done(
    asana_stub, monkeypatch
):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    recurrence.spawn_next(
        _task(), _detail(), {"gid": "sec-done", "name": "Done"}, ("t2", relativedelta(months=3))
    )
    assert asana_stub["sections"] == []


def test_spawn_next_strips_the_tag_and_links_forward(asana_stub, monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    recurrence.spawn_next(_task(), _detail(), None, ("t2", relativedelta(months=3)))

    assert asana_stub["removed_tags"] == [("old-1", "t2")]
    gid, kwargs = asana_stub["stories"][0]
    assert gid == "old-1"
    assert "https://app.asana.com/0/1/new-1" in kwargs["text"]
    assert asana_stub["refreshed"] == ["new-1"]


def test_spawn_next_is_idempotent_via_the_external_gid(asana_stub, monkeypatch):
    monkeypatch.setattr(asana, "find_task_by_external", lambda ext: "already-there")
    assert recurrence.spawn_next(_task(), _detail(), None, ("t2", relativedelta(months=3))) is None
    assert asana_stub["created"] == []
    assert asana_stub["removed_tags"] == []


def test_a_failed_tag_strip_does_not_lose_the_successor(asana_stub, monkeypatch):
    def boom(task_gid, tag_gid):
        raise RuntimeError("asana down")

    monkeypatch.setattr(asana, "remove_tag", boom)
    assert (
        recurrence.spawn_next(_task(), _detail(), None, ("t2", relativedelta(months=3))) == "new-1"
    )


def test_spawn_next_copies_no_comments_or_subtasks(asana_stub):
    recurrence.spawn_next(_task(), _detail(num_subtasks=3), None, ("t2", relativedelta(months=3)))
    fields = asana_stub["created"][0]
    assert "parent" not in fields
    assert "num_subtasks" not in fields
    assert "due_at" not in fields
    assert "completed" not in fields

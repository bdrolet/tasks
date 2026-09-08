from models.events import Screening
from services import shared_tags
from tests.test_events import make_email_event


def test_addresses_from_env_parses_and_casefolds(monkeypatch):
    monkeypatch.setenv("CHERYL_EMAILS", " Cheryl@Example.com, c2@example.org ,,")
    assert shared_tags.addresses_from_env() == {"cheryl@example.com", "c2@example.org"}
    monkeypatch.delenv("CHERYL_EMAILS", raising=False)
    assert shared_tags.addresses_from_env() == set()


def test_cheryl_on_email_matches_sender_to_or_cc_case_insensitively():
    addrs = {"cheryl@example.com"}
    assert shared_tags.cheryl_on_email(make_email_event(sender="CHERYL@example.com"), addrs)
    assert shared_tags.cheryl_on_email(make_email_event(to=["ben@x", "cheryl@example.com"]), addrs)
    assert shared_tags.cheryl_on_email(make_email_event(cc=["Cheryl@Example.com"]), addrs)
    assert not shared_tags.cheryl_on_email(make_email_event(sender="other@example.com"), addrs)
    assert not shared_tags.cheryl_on_email(make_email_event(cc=["cheryl@example.com"]), set())


def test_for_event_keeps_inbox_tags_and_adds_cheryl_on_shared_audience():
    event = make_email_event(tags=["finance"])
    verdict = Screening(priority="P1", audience="shared")
    assert shared_tags.for_event(event, verdict, addresses=set()) == ["finance", "cheryl"]


def test_for_event_adds_cheryl_on_address_hit():
    event = make_email_event(tags=["finance"], cc=["cheryl@example.com"])
    verdict = Screening(priority="P1", audience="self")
    out = shared_tags.for_event(event, verdict, addresses={"cheryl@example.com"})
    assert out == ["finance", "cheryl"]


def test_for_event_does_not_duplicate_or_invent():
    verdict_shared = Screening(priority="P1", audience="shared")
    assert shared_tags.for_event(
        make_email_event(tags=["cheryl"]), verdict_shared, addresses=set()
    ) == ["cheryl"]
    verdict_self = Screening(priority="P1", audience="self")
    assert shared_tags.for_event(
        make_email_event(tags=["finance"]), verdict_self, addresses=set()
    ) == ["finance"]
    assert shared_tags.for_event(make_email_event(tags=None), verdict_self, addresses=set()) == []

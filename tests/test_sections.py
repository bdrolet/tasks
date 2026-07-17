from services import sections


def test_for_category(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    monkeypatch.setenv("ASANA_SECTION_RESPOND_GID", "sec-respond")
    assert sections.for_category("review") == "sec-review"
    assert sections.for_category("respond") == "sec-respond"


def test_unknown_category_returns_none(monkeypatch):
    assert sections.for_category("ignore") is None


def test_unset_env_returns_none(monkeypatch):
    monkeypatch.delenv("ASANA_SECTION_REVIEW_GID", raising=False)
    assert sections.for_category("review") is None


def test_done_and_overdue(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_DONE_GID", "sec-done")
    monkeypatch.setenv("ASANA_SECTION_OVERDUE_GID", "sec-overdue")
    assert sections.done() == "sec-done"
    assert sections.overdue() == "sec-overdue"

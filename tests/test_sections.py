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


def test_rescued_categories_default_to_the_review_section(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    assert sections.for_category("ignore", default=True) == "sec-review"
    assert sections.for_category("reference", default=True) == "sec-review"
    assert sections.for_category("", default=True) == "sec-review"


def test_the_label_path_does_not_default_to_review(monkeypatch):
    """handlers/label_applied.py passes a label: 'ignore'/'reference' mean
    'no section move', not 'move this task into Review'."""
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    assert sections.for_category("ignore") is None
    assert sections.for_category("reference") is None


def test_urgent_still_unsectioned_when_its_gid_is_unset(monkeypatch):
    monkeypatch.setenv("ASANA_SECTION_REVIEW_GID", "sec-review")
    monkeypatch.delenv("ASANA_SECTION_URGENT_GID", raising=False)
    assert sections.for_category("urgent", default=True) is None

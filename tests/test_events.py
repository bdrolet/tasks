from models.events import CreatedTask, EmailClassifiedEvent, EmailSummary, LabelAppliedEvent


def make_email_event(**overrides) -> EmailClassifiedEvent:
    event: EmailClassifiedEvent = {
        "event": "email_classified",
        "message_id": "msg-123",
        "category": "review",
        "importance": "P1",
        "confidence": 0.92,
        "subject": "Quarterly report",
        "sender": "alice@example.com",
        "sender_display": "Alice",
        "to": ["ben@drolet.cloud"],
        "cc": ["team@example.com"],
        "received_at": "2026-07-15T12:00:00Z",
        "tags": ["finance"],
        "reasoning": "Needs review",
        "body": "Please review the attached report before Friday. " * 5,
        "body_html": '<p>Please review <a href="https://docs.example/q2">the Q2 report</a></p>',
        "web_link": "https://outlook.example/msg-123",
    }
    event.update(overrides)  # type: ignore[typeddict-item]
    return event


def test_email_event_optional_fields_absent():
    event = make_email_event()
    assert event.get("draft_link") is None
    assert event.get("seed_key_points") is None
    assert event.get("seed_links") is None


def test_email_event_carries_all_categories():
    for category in ("urgent", "respond", "review", "reference", "ignore"):
        assert make_email_event(category=category)["category"] == category


def test_label_applied_event_allows_null_task_gid():
    event: LabelAppliedEvent = {
        "event": "label_applied",
        "message_id": "msg-123",
        "task_gid": None,
        "label": "respond",
        "source": "human_correction",
    }
    assert event["task_gid"] is None


def test_summary_and_created_task():
    summary = EmailSummary()
    assert summary.key_points == [] and summary.relevant_links == []
    task = CreatedTask(gid="42", permalink_url="https://app.asana.com/0/1/42")
    assert task.gid == "42"

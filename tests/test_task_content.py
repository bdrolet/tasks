from models.task_content import Source, TaskContent


def test_taskcontent_defaults_are_empty():
    c = TaskContent()
    assert c.context is None
    assert c.key_points == []
    assert c.links == []
    assert c.action_items == []
    assert c.source is None


def test_source_defaults():
    s = Source(origin="Email")
    assert s.origin == "Email"
    assert s.rows == []
    assert s.links == []
    assert s.note is None

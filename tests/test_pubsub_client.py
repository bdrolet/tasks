import json

import clients.pubsub as ps


class _Future:
    def result(self, timeout=None):
        return "msg-1"


class _Publisher:
    def __init__(self):
        self.calls = []

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, path, data, **attrs):
        self.calls.append((path, json.loads(data), attrs))
        return _Future()


def test_publish_encodes_json_and_blocks(monkeypatch):
    pub = _Publisher()
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setattr(ps, "_client", lambda topic: (pub, pub.topic_path("proj", topic)))
    ps.publish("task-events", {"kind": "task_changed", "gid": "t1"})
    path, body, _ = pub.calls[0]
    assert path.endswith("/topics/task-events") and body["gid"] == "t1"


def test_publish_task_changed_is_best_effort(monkeypatch, caplog):
    def boom(topic, event):
        raise RuntimeError("no broker")

    monkeypatch.setattr(ps, "publish", boom)
    ps.publish_task_changed("t1", "webhook")  # must not raise
    assert "task_changed publish failed" in caplog.text


def test_publish_task_changed_shape(monkeypatch):
    seen = []
    monkeypatch.setattr(ps, "publish", lambda topic, event: seen.append((topic, event)))
    ps.publish_task_changed("t1", "api")
    assert seen == [("task-events", {"kind": "task_changed", "gid": "t1", "source": "api"})]

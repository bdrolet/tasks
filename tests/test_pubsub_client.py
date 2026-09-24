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


class _RecordingFuture:
    """Records the timeout it is asked to wait under; `.result()` returns
    immediately (no real blocking in tests)."""

    def __init__(self, timeouts: list, raises: bool = False):
        self._timeouts = timeouts
        self._raises = raises

    def result(self, timeout=None):
        self._timeouts.append(timeout)
        if self._raises:
            raise RuntimeError("broker timeout")
        return "msg-id"


class _RecordingPublisher:
    def __init__(self):
        self.calls = []

    def topic_path(self, project, topic):
        return f"projects/{project}/topics/{topic}"

    def publish(self, path, data, **attrs):
        self.calls.append((path, json.loads(data), attrs))
        return self._next_future()

    def _next_future(self):
        raise NotImplementedError


def test_publish_many_sends_every_event_and_returns_acked_count(monkeypatch):
    timeouts: list = []
    pub = _RecordingPublisher()
    pub._next_future = lambda: _RecordingFuture(timeouts)
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setattr(ps, "_client", lambda topic: (pub, pub.topic_path("proj", topic)))

    events = [{"kind": "task_changed", "gid": g} for g in ("t1", "t2", "t3")]
    acked = ps.publish_many("task-events", events, deadline_s=5.0)

    assert acked == 3
    assert [c[1]["gid"] for c in pub.calls] == ["t1", "t2", "t3"]
    # every future's timeout is under the deadline, and later futures get a
    # shrinking (never growing) budget as the deadline approaches
    assert all(t is not None and t <= 5.0 for t in timeouts)
    assert timeouts == sorted(timeouts, reverse=True)


def test_publish_many_logs_and_excludes_a_failed_future(monkeypatch, caplog):
    timeouts: list = []
    pub = _RecordingPublisher()
    calls = {"n": 0}

    def next_future():
        calls["n"] += 1
        return _RecordingFuture(timeouts, raises=(calls["n"] == 2))

    pub._next_future = next_future
    monkeypatch.setenv("GCP_PROJECT_ID", "proj")
    monkeypatch.setattr(ps, "_client", lambda topic: (pub, pub.topic_path("proj", topic)))

    events = [{"kind": "task_changed", "gid": g} for g in ("t1", "t2", "t3")]
    acked = ps.publish_many("task-events", events, deadline_s=5.0)

    assert acked == 2  # t2's future raised — must not raise into the caller
    assert "publish to task-events failed or timed out" in caplog.text
    assert "2/3 publishes acked" in caplog.text


def test_publish_many_empty_events_is_a_noop(monkeypatch):
    def boom(topic):
        raise AssertionError("must not touch the client for an empty batch")

    monkeypatch.setattr(ps, "_client", boom)
    assert ps.publish_many("task-events", []) == 0


def test_publish_task_changed_many_shapes_events_and_returns_acked(monkeypatch):
    seen = {}

    def fake_publish_many(topic, events, *, deadline_s=5.0):
        seen["topic"] = topic
        seen["events"] = events
        seen["deadline_s"] = deadline_s
        return len(events)

    monkeypatch.setattr(ps, "publish_many", fake_publish_many)
    acked = ps.publish_task_changed_many(["t1", "t2"], "webhook")
    assert acked == 2
    assert seen["topic"] == ps.TASK_EVENTS
    assert seen["events"] == [
        {"kind": "task_changed", "gid": "t1", "source": "webhook"},
        {"kind": "task_changed", "gid": "t2", "source": "webhook"},
    ]


def test_publish_task_changed_many_is_best_effort(monkeypatch, caplog):
    def boom(topic, events, *, deadline_s=5.0):
        raise RuntimeError("no broker")

    monkeypatch.setattr(ps, "publish_many", boom)
    acked = ps.publish_task_changed_many(["t1"], "webhook")  # must not raise
    assert acked == 0
    assert "task_changed batch publish failed" in caplog.text

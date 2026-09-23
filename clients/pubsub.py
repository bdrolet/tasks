"""Thin Pub/Sub publisher — I/O only; payload shape belongs to the caller.
Port of inbox clients/pubsub.py. The google-cloud-pubsub import is lazy so
the test suite and requirements-dev.txt stay free of it."""

import json
import logging
import os

from opentelemetry.propagate import inject

logger = logging.getLogger(__name__)

TASK_EVENTS = "task-events"

_publisher = None
_topic_paths: dict[str, str] = {}


def _client(topic: str):
    global _publisher
    from google.cloud import pubsub_v1

    if _publisher is None:
        _publisher = pubsub_v1.PublisherClient()
    if topic not in _topic_paths:
        _topic_paths[topic] = _publisher.topic_path(os.environ["GCP_PROJECT_ID"], topic)
    return _publisher, _topic_paths[topic]


def publish(topic: str, event: dict) -> None:
    """Publish a JSON event with trace context as attributes. Blocks until the
    broker acks: the publisher batches on a background thread and a
    scale-to-zero function exiting first would drop the message silently."""
    publisher, path = _client(topic)
    carrier: dict = {}
    inject(carrier)
    publisher.publish(path, json.dumps(event).encode(), **carrier).result(timeout=30)


def publish_task_changed(gid: str, source: str) -> None:
    """Best-effort: a dropped event is healed by the daily tick (spec D8)."""
    try:
        publish(TASK_EVENTS, {"kind": "task_changed", "gid": gid, "source": source})
    except Exception:
        logger.exception("task_changed publish failed for gid=%s source=%s", gid, source)

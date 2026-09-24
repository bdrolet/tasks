"""Thin Pub/Sub publisher — I/O only; payload shape belongs to the caller.
Port of inbox clients/pubsub.py. The google-cloud-pubsub import is lazy so
the test suite and requirements-dev.txt stay free of it."""

import json
import logging
import os
import time

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


def publish_many(topic: str, events: list[dict], *, deadline_s: float = 5.0) -> int:
    """Publish a batch and wait for all acks under ONE deadline. Returns the
    number acked. Best-effort: a failed or late publish is logged, never
    raised — the daily heal republishes anything dropped (spec D8)."""
    if not events:
        return 0
    publisher, path = _client(topic)
    carrier: dict = {}
    inject(carrier)
    futures = [publisher.publish(path, json.dumps(e).encode(), **carrier) for e in events]
    deadline = time.monotonic() + deadline_s
    acked = 0
    for future in futures:
        try:
            future.result(timeout=max(0.0, deadline - time.monotonic()))
            acked += 1
        except Exception:
            logger.exception("publish to %s failed or timed out", topic)
    if acked < len(events):
        logger.warning("%d/%d publishes acked before the deadline", acked, len(events))
    return acked


def publish_task_changed_many(gids: list[str], source: str, *, deadline_s: float = 5.0) -> int:
    """Best-effort batch of publish_task_changed — one shared deadline rather
    than N sequential 30s waits (spec D8: a webhook delivery has ~10s to
    reply, and the per-gid refresh cap exists to protect that budget)."""
    try:
        return publish_many(
            TASK_EVENTS,
            [{"kind": "task_changed", "gid": g, "source": source} for g in gids],
            deadline_s=deadline_s,
        )
    except Exception:
        logger.exception("task_changed batch publish failed source=%s", source)
        return 0

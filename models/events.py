"""Typed payloads for events arriving on the email-events Pub/Sub topic.

Mirrors what inbox publishes (see inbox services/email_events.py). This is a
domain event — "an email was classified" — not a command; services/policy.py
decides whether it becomes a task. JSON has no tuples, so link pairs arrive as
[url, label] lists.
"""

from dataclasses import dataclass, field
from typing import Literal, NotRequired, TypedDict


class EmailClassifiedEvent(TypedDict):
    event: Literal["email_classified"]
    message_id: str
    category: str  # "urgent" | "respond" | "review" | "reference" | "ignore"
    importance: str  # "P0" | "P1" | "P2" | "P3"
    confidence: float
    subject: str
    sender: str
    sender_display: str
    to: list[str]  # recipient addresses
    cc: list[str]
    received_at: str
    tags: list[str]
    reasoning: str
    body: str  # plain text; inbox truncates to 10k chars
    body_html: str | None  # for link extraction; inbox truncates to 200k chars
    web_link: str | None
    draft_link: NotRequired[str | None]  # respond only
    seed_key_points: NotRequired[list[str] | None]  # invite facts from inbox
    seed_links: NotRequired[list[list[str]] | None]  # invite/RSVP [url, label] pairs


class LabelAppliedEvent(TypedDict):
    event: Literal["label_applied"]
    message_id: str
    task_gid: str | None  # None → resolve via DB, then external:{message_id}
    label: str
    source: str


@dataclass
class EmailSummary:
    key_points: list[str] = field(default_factory=list)
    relevant_links: list[list[str]] = field(default_factory=list)  # [url, label]


@dataclass
class CreatedTask:
    gid: str
    permalink_url: str

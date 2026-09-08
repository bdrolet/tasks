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
    # Published by inbox since the schedule extraction; declared here so the
    # screener can reach attachments. graph_message_id is the IMMUTABLE GRAPH
    # id — message_id is inbox's internal UUID and inbox-api rejects it with
    # ErrorInvalidIdMalformed.
    graph_message_id: NotRequired[str]
    has_attachments: NotRequired[bool]


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
    title: str | None = None  # normalized "{verb} {object}", no [PX] prefix


@dataclass
class CreatedTask:
    gid: str
    permalink_url: str


@dataclass
class Decision:
    """Gate-2 verdict from services/triage.py. Defaults ARE the fail-open
    state: actionable, no reason, no related task."""

    actionable: bool = True
    reason: str = ""
    related_task_gid: str | None = None
    resolves: bool = False  # related task's matter is settled by this email
    evidence: list = field(default_factory=list)
    outcome: str = "actionable"  # actionable | suppressed | attached | fail_open


@dataclass
class Screening:
    """Gate-1 verdict from services/screening.py.

    Three-way by design: `drop` and `relate` are both "no task", but they are
    not the same answer. `relate` means the email needs no work of its own AND
    plausibly reports on something already tracked — a confirmation, a receipt,
    a "your request was processed". services/relating.py turns that into a
    comment. Collapsing the two loses the only path to _suppress()'s
    related-task branch.

    Defaults ARE the fail-open state: it is a task, at middling priority."""

    verdict: str = "task"  # task | relate | drop
    priority: str = "P2"  # P0 | P1 | P2 | P3
    reason: str = ""
    audience: str = "self"  # self | shared — shared = held jointly by Ben's household
    outcome: str = "task"  # task | relate | drop | fail_open

    @property
    def is_task(self) -> bool:
        return self.verdict == "task"


@dataclass
class Match:
    """Result of services/relating.py. Defaults ARE the no-match state, which
    is a normal outcome: the email is recorded as a plain suppression and no
    comment is posted."""

    task_gid: str | None = None
    resolves: bool = False
    reason: str = ""
    evidence: list = field(default_factory=list)

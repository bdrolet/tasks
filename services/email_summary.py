"""Claude Haiku enrichment for a qualifying email: key points, relevant links,
and an actionable task title.

Task titles MUST follow the "Title" section of docs/task-content-standard.md —
that section is authoritative; if this code and the doc ever disagree, the DOC
WINS (fix the code). In brief: "{verb} {object}", imperative verb first, sentence
case, no trailing punctuation, <= ~60 chars, and NO [PX] prefix (the caller adds
it).
"""

import json
import logging
import re
from html.parser import HTMLParser

import clients.claude as claude
from models.events import EmailClassifiedEvent, EmailSummary

logger = logging.getLogger(__name__)

_NOISE = re.compile(
    r"unsubscribe|tracking|pixel|open.?in.?browser|view.?online|manage.?preferences"
    r"|groups\.google\.com.*msgid|utm_",
    re.IGNORECASE,
)
_GENERIC_LABELS = {"click here", "here", "link", "this link", "more", "read more"}

MAX_TITLE_CHARS = 60


def _normalize_title(raw: str | None) -> str | None:
    """Clean a model-produced "{verb} {object}" title per the Title section of
    docs/task-content-standard.md (authoritative — doc wins). None if empty."""
    if not raw:
        return None
    title = " ".join(raw.split()).rstrip(".!,;:")
    if len(title) > MAX_TITLE_CHARS:
        title = title[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip()
    return title or None


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href.startswith("http"):
                self._href = href
                self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href:
            label = " ".join("".join(self._text).split())
            if (
                label
                and label.lower() not in _GENERIC_LABELS
                and not _NOISE.search(label)
                and not _NOISE.search(self._href)
            ):
                self.links.append((self._href, label))
            self._href = None
            self._text = []


def _extract_links(html: str) -> list[tuple[str, str]]:
    extractor = _LinkExtractor()
    try:
        extractor.feed(html)
    except Exception:
        return []
    seen: set[str] = set()
    result = []
    for url, label in extractor.links:
        if url not in seen:
            seen.add(url)
            result.append((url, label))
    return result


def generate(event: EmailClassifiedEvent) -> EmailSummary:
    """Return key points and relevant links for the email."""
    links = [[url, label] for url, label in _extract_links(event.get("body_html") or "")]

    body_text = (event["body"] or "")[:3000]
    prompt = (
        "Summarize this email in 2-3 concise bullet points, and write a task "
        "title for it. Be specific about what action is requested or what "
        "information is conveyed. No preamble.\n"
        "The title must be an actionable next step: start with an imperative "
        "verb, then 2-5 words naming what the action is on. Sentence case, no "
        "trailing punctuation, 8 words max. Do NOT include a priority tag. "
        'Examples: "Review Q3 board deck", "Reply to Sarah on contract redlines".\n'
        'Return JSON only: {"key_points": ["point 1", "point 2"], "title": "Verb object"}\n\n'
        f"Subject: {event['subject']}\n"
        f"From: {event['sender_display'] or event['sender']}\n\n"
        f"{body_text}"
    )
    key_points: list[str] = []
    title: str | None = None
    try:
        raw = claude.summarize(prompt)
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        data = json.loads(raw)
        key_points = data.get("key_points", [])
        title = _normalize_title(data.get("title"))
    except Exception:
        logger.warning("Email summary generation failed for message_id=%s", event["message_id"])

    return EmailSummary(key_points=key_points, relevant_links=links, title=title)

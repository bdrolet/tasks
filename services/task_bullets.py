"""Per-task bullets and doc links for the due-day digest.

Links are deterministic: anchors inside the description's `Links:` block
(the block services/task_content.py renders between `Key points:` and
`Actions`). Actions are label-webhook buttons and Source is the originating
email — neither is a document. Bullets are Claude-condensed (Task 6) with a
deterministic fallback defined here. Design: D6 in
docs/superpowers/specs/2026-09-03-due-day-digest-design.md."""

import hashlib
from html.parser import HTMLParser

MAX_LINKS = 5
POINT_CHARS = 140

_STOP_HEADINGS = ("actions", "source:")


class _NotesParser(HTMLParser):
    """One pass over html_notes: text of the substance (before Actions/Source),
    the Key points list items, and the anchors inside the Links block."""

    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.key_points: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._in_strong = False
        self._block = ""  # "", "key_points", "links", "stop"
        self._href: str | None = None
        self._anchor: list[str] = []
        self._li: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._block == "stop":
            return
        if tag == "strong":
            self._in_strong = True
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._anchor = []
        elif tag == "li":
            self._li = []

    def handle_endtag(self, tag: str) -> None:
        if self._block == "stop":
            return
        if tag in ("li", "ul", "strong", "p", "div", "br"):
            self.text.append(" ")  # keep words apart when tags are the only separator
        if tag == "strong":
            self._in_strong = False
        elif tag == "a":
            if self._block == "links" and self._href:
                label = " ".join("".join(self._anchor).split())
                self.links.append((self._href, label or self._href))
            self._href = None
        elif tag == "li":
            if self._block == "key_points" and self._li is not None:
                item = " ".join("".join(self._li).split())
                if item:
                    self.key_points.append(item)
            self._li = None

    def handle_data(self, data: str) -> None:
        if self._block == "stop":
            return
        if self._in_strong:
            heading = data.strip().casefold()
            if heading.startswith(_STOP_HEADINGS):
                self._block = "stop"
                return
            if heading.startswith("key points"):
                self._block = "key_points"
            elif heading.startswith("links"):
                self._block = "links"
            else:
                self._block = ""
        self.text.append(data)
        if self._href is not None:
            self._anchor.append(data)
        if self._li is not None:
            self._li.append(data)


def _parse(html_notes: str) -> _NotesParser:
    p = _NotesParser()
    try:
        p.feed(html_notes or "")
    except Exception:
        return _NotesParser()
    return p


def parse_links(html_notes: str) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for url, label in _parse(html_notes).links:
        if url in seen:
            continue
        seen.add(url)
        out.append((url, label))
        if len(out) == MAX_LINKS:
            break
    return out


def description_text(html_notes: str) -> str:
    return " ".join("".join(_parse(html_notes).text).split())


def _clip(text: str, limit: int = POINT_CHARS) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return head.rstrip(" ,;:.") + "…"


def fallback_points(html_notes: str) -> list[str]:
    """First two key points; else the lead prose clipped; else nothing."""
    parsed = _parse(html_notes)
    if parsed.key_points:
        return [_clip(p) for p in parsed.key_points[:2]]
    lead = " ".join("".join(parsed.text).split())
    for marker in ("Key points:", "Links:"):
        if marker in lead:
            lead = lead.split(marker, 1)[0].strip()
    return [_clip(lead)] if lead else []


def content_hash(name: str, html_notes: str) -> str:
    return hashlib.sha256(f"{name}\n{html_notes or ''}".encode()).hexdigest()

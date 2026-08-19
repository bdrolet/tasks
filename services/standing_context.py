"""Declared facts about Ben — context/standing-context.md, sectioned by
'## ' headings. Consumers read only the section they need. Any read failure
yields "" so a missing/unreadable file degrades to 'no facts', never an error."""

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "context" / "standing-context.md"
PATH = Path(os.environ.get("STANDING_CONTEXT_PATH", str(DEFAULT_PATH)))

_cache: str | None = None
_H2 = re.compile(r"^## ", re.MULTILINE)


def reset_cache() -> None:
    global _cache
    _cache = None


def _text() -> str:
    global _cache
    if _cache is None:
        try:
            _cache = PATH.read_text(encoding="utf-8")
        except Exception:
            logger.warning("standing context unreadable at %s — treating as empty", PATH)
            _cache = ""
    return _cache


def section(name: str, *, text: str | None = None) -> str:
    """Body of the `## {name}` section (heading match is case-insensitive),
    stripped; "" if absent. `text` overrides the file (tests, previews)."""
    source = _text() if text is None else text
    for chunk in _H2.split(source)[1:]:
        heading, _, body = chunk.partition("\n")
        if heading.strip().casefold() == name.casefold():
            return body.strip()
    return ""

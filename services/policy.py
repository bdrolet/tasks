"""Task policy: which classified emails become Asana tasks.

Inbox classifies; this module decides. Changing what becomes a task happens
here — no inbox deploy needed. Urgent is included to match pre-extraction
inbox behavior (urgent.handle created tasks too)."""

import re

from models.events import EmailClassifiedEvent

_TASK_CATEGORIES = {"urgent", "review", "respond"}


def warrants_task(event: EmailClassifiedEvent) -> bool:
    return event.get("category") in _TASK_CATEGORIES


# Deterministic backstop for gate 2 (docs/no_action_needed_example.md): if the
# enrichment's own key points contain an explicit no-action phrase, do not
# create the task. Free to test; catches what the agent missed when the
# summarizer wrote the disqualifier down anyway. This runs AFTER the triage
# agent's actionable verdict — a false positive here silently overrides a
# correct "actionable" call and swallows a real task, so it must fail open
# whenever the key point carries any hint that something still needs doing.
#
# Two tiers:
# - Unconditional: an explicit "no action required/needed" phrase is a hard
#   stop per the spec — it vetoes regardless of what else is in the point.
# - Conditional: "automatic payment" / "autopay" / "for your records" only
#   mean no action is needed when the SAME key point describes routine,
#   successful processing or a genuine no-op — not when it's attached to a
#   failure, a live obligation, or a deadline (see _DISQUALIFIER). The design
#   spec lists these as flat, unconditional patterns, but a flat match also
#   swallows things like "automatic payment failed, update your card by
#   Friday" or "Sign and return the form; keep a copy for your records" —
#   which must still become a task. Per the governing principle ("a spurious
#   task costs seconds to close; a swallowed message about a child's team
#   placement is unbounded"), deliberate deviation from the spec's flat list.
UNCONDITIONAL_NO_ACTION_PATTERNS = [
    re.compile(r"no action (?:is )?(?:required|needed)", re.IGNORECASE),
]

CONDITIONAL_NO_ACTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"automatic payment",
        r"\bautopay\b",
        r"for your records",
    )
]

# Disqualifies a conditional match on the same key point — presence of any
# of these means the point is describing trouble, or a live obligation, not
# routine/no-op processing. Includes both failure/attention-needed language
# and obligation language (an ask attached to "for your records" must not be
# swallowed by it).
_DISQUALIFIER = re.compile(
    r"fail(?:ed|ure)?|declin(?:e|ed)|unsuccessful|could ?n[o']t|cannot|can't|"
    r"unable|expir(?:ed|es|ing)|overdue|past due|update your|"
    r"action (?:is )?required|retry|returned|reject(?:ed)?|insufficient|"
    r"\bsign\b|\bsubmit\b|\brsvp\b|\breply\b|\brespond\b|\bconfirm\b|\bdue\b|\bdeadline\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return " ".join(text.split())[:300]


def no_action_phrase(key_points: list[str]) -> str | None:
    """The key point that trips a no-action pattern, else None.

    Returns the whole matching key point — whitespace-normalised, capped at
    ~300 chars — rather than just the matched phrase, so the suppression
    record it feeds (handlers/task_create.py::_suppress) is enough on its own
    to audit what the veto ate.

    Backstop applied after the triage agent's actionable verdict, so a false
    positive here costs a real task: the conditional patterns (autopay /
    automatic payment / for your records) only veto a key point that carries
    no failure or obligation language — see CONDITIONAL_NO_ACTION_PATTERNS
    and _DISQUALIFIER.
    """
    for point in key_points:
        text = point or ""
        for pat in UNCONDITIONAL_NO_ACTION_PATTERNS:
            if pat.search(text):
                return _normalize(text)
        for pat in CONDITIONAL_NO_ACTION_PATTERNS:
            if pat.search(text) and not _DISQUALIFIER.search(text):
                return _normalize(text)
    return None

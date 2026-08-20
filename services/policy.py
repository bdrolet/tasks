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
UNCONDITIONAL_NO_ACTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"no action (?:is )?(?:required|needed)",
        r"for your records",
    )
]

# Conditional: "automatic payment" / "autopay" only mean no action needed
# when they describe routine, successful processing (the spec's own Xfinity
# evidence case). The design spec lists these as flat, unconditional
# patterns, but a flat match also swallows a *failure* notice — "automatic
# payment failed, update your card by Friday" — which must still become a
# task. Per the governing principle ("a spurious task costs seconds to
# close; a swallowed message about a child's team placement is unbounded"),
# these two patterns veto only when the SAME key point contains no
# failure/attention-needed language; deliberate deviation from the spec's
# flat list.
CONDITIONAL_NO_ACTION_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"automatic payment",
        r"\bautopay\b",
    )
]

# Disqualifies a conditional match on the same key point — presence of any
# of these means the point is describing trouble, not routine processing.
_DISQUALIFIER = re.compile(
    r"fail(?:ed|ure)?|declin(?:e|ed)|unsuccessful|could ?n[o']t|cannot|can't|"
    r"unable|expir(?:ed|es|ing)|overdue|past due|update your|"
    r"action (?:is )?required|retry|returned|reject(?:ed)?|insufficient",
    re.IGNORECASE,
)


def no_action_phrase(key_points: list[str]) -> str | None:
    """The first no-action phrase found in the generated key points, else None.

    Backstop applied after the triage agent's actionable verdict, so a false
    positive here costs a real task: the conditional patterns (autopay /
    automatic payment) only veto a key point that carries no failure or
    attention-needed language — see CONDITIONAL_NO_ACTION_PATTERNS.
    """
    for point in key_points:
        text = point or ""
        for pat in UNCONDITIONAL_NO_ACTION_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(0).lower()
        for pat in CONDITIONAL_NO_ACTION_PATTERNS:
            m = pat.search(text)
            if m and not _DISQUALIFIER.search(text):
                return m.group(0).lower()
    return None

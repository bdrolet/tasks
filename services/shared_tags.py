"""The `cheryl` tag for email-derived tasks — the signal the due-day digest
routes to the shared "Ben | Cheryl" calendar (CLAUDE.md → Due-day digest).

Two triggers, either one suffices:
  - the gate-1 screener judged the matter `shared` (services/screening.py,
    against the private household facts), or
  - Cheryl herself is on the email — sender, to, or cc — per `CHERYL_EMAILS`,
    a comma-separated list from the gitignored tfvars (personal; never here).

Pure: no I/O beyond reading the env var."""

import os

from models.events import EmailClassifiedEvent, Screening

TAG = "cheryl"
ENV = "CHERYL_EMAILS"


def addresses_from_env() -> set[str]:
    raw = os.environ.get(ENV, "")
    return {a.strip().casefold() for a in raw.split(",") if a.strip()}


def cheryl_on_email(event: EmailClassifiedEvent, addresses: set[str]) -> bool:
    if not addresses:
        return False
    on_email = (
        [event.get("sender") or ""] + list(event.get("to") or []) + list(event.get("cc") or [])
    )
    return any(a.strip().casefold() in addresses for a in on_email)


def for_event(event: EmailClassifiedEvent, verdict: Screening, *, addresses: set[str]) -> list[str]:
    """Inbox's tags, plus `cheryl` when either trigger fires. Order preserved,
    no duplicates."""
    names = list(event.get("tags") or [])
    if (verdict.audience == "shared" or cheryl_on_email(event, addresses)) and TAG not in names:
        names.append(TAG)
    return names

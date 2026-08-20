# Standing context

Declared facts about Ben that the pipeline cannot derive from mail. Read by
`services/standing_context.py`; sections are addressed by `## ` heading.
`Roles` feeds the triage agent (services/triage.py); `Calendar` feeds
deadline extraction (services/deadline.py). A fact that applies only for a
period states that period in its own prose — the model is given today's date
and decides whether it still applies. A bounded fact must say, as a direct
instruction, when it stops applying — leaving the end date as background
detail reads as permanent to the model, as live testing on the coach-role
fact below confirmed. Retire a fact by deleting its block.

## Roles

### Assistant Coach, West Portal Proud Panthers (SF Microsoccer / SF Vikings)
Ben resigned this role on 2026-08-14, covering only the 2026 fall season
(2026-09-12 to 2026-12-18); Christy Dillon is handling the replacement.
**This fact stops applying after 2026-12-18 — judge coach- and
admin-directed mail on its own terms from that date on.** Elijah remains a
player on the team throughout, unaffected by this fact's expiry.

- Through 2026-12-18, coach- and admin-directed mail — schedules to review,
  coach admin requirements, Micro Admin broadcasts — is NOT actionable.
- Mail about Elijah as a player — invitations, rosters, parent logistics —
  IS actionable at any time.

## Calendar

- SFUSD 2026-27 fall term: 2026-08-17 to 2026-12-18.
- West Portal Elementary day: 7:50am-2:05pm Mon/Tue/Thu/Fri, 7:50am-12:50pm Wed.

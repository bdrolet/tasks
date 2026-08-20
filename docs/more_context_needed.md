# More context needed

A running log of tasks that were created — and passed the policy gate cleanly —
but that a human immediately recognized as **moot**. In every case the email
itself was correctly classified; what was missing is context that lives
*outside* the triggering email: prior mail in the same account, or a task
already in Asana.

> **Status (2026-08-19):** addressed by gate 2 — see
> `docs/superpowers/specs/2026-08-18-standing-context-gate-design.md` and
> `services/triage.py`. Keep logging new cases here; they are the eval set.

These are not classification bugs. `services/policy.py::warrants_task` looks at
one event in isolation, and by that standard each of these was a reasonable
task. The failure is at a different altitude: the pipeline never asks *"given
everything else I know, does this still need doing?"*

Use this file as the evidence base for a pre-creation context check. Add a new
entry whenever a task turns out to have been answerable from context the
pipeline already had access to.

## The two checks these examples call for

1. **Mail-history check** — before creating a task whose action is a recurring
   obligation (a bill, a renewal, a subscription), search the mailbox for prior
   messages on the same thread/sender that show the obligation is already
   automated or already satisfied.
2. **Existing-task check** — before creating a task from an email, search Asana
   (including **completed** tasks) for the same underlying matter. A follow-up
   email in an ongoing saga usually belongs as a comment on the existing task,
   or as nothing at all.

Both checks are available today: `POST /search` on the tasks-api
(`completed: null` searches open and done together, `semantic: true` for
conceptual matches) and `POST /search` on the inbox-api (`mode: graph` for live
Outlook).

---

## Example 1 — Xfinity bill: autopay made the task moot

**Task:** `[P1] Review and pay Xfinity bill` (gid `1217530946487945`), created
2026-08-15 from the "Your bill statement is available to view and pay online"
email. Due 2026-09-09, amount $70.00.

**What the pipeline saw:** a bill notice with an amount and a due date. Category
`review`, importance P1. Correct on its face — an unpaid bill with a deadline is
exactly what the policy gate is for.

**What a mail-history check would have found:**

- 2026-08-10, Xfinity, *"Thanks for your payment"* — "Your **automatic payment**
  was processed successfully. Account number: Ending in 8242. Payment amount:
  $70.00."
- 2026-07-15, Xfinity, *"Your bill is available to view online"* — body header
  reads "Your **automatic payment details** … Payment date: 2026-08-09."
- The triggering email itself carries the same "Your automatic payment details"
  block above the amount due.

**Why it matters:** the $70 on 2026-09-09 will draw automatically. There is no
action. The task's own source email contained the disqualifying evidence, and
one search for `Xfinity` would have surfaced two more months of it.

**Signal to look for:** the phrase "automatic payment" (or "autopay",
"scheduled payment", "will be charged automatically") anywhere in the current
email or in recent mail from the same sender. When present, the right output is
a P3 reference note or nothing — not a P1 payment task.

## Example 2 — Disney Plus refund: the saga was already closed

**Task:** `[P1] Review Disney Plus refund confirmation` (gid `1217504339696293`),
created from the "partial refund of $24.98 USD to your Mastercard ending in
6271" email. No due date.

**What the pipeline saw:** a refund notice mentioning money and an account.
Category `review`, importance P1.

**What an existing-task check would have found** — two tasks from the same
refund, both already **completed**:

| Task | GID | State |
|---|---|---|
| `[P1] Follow up on delayed Disney Plus refund` ($24.98) | `1217290596630525` | ✓ completed |
| `[P1] Resolve failed Disney Plus refund` ($24.58, txn `01X39478N0759190M`) | `1217290596663963` | ✓ completed |

A single search would have surfaced both:

```bash
curl -s -X POST https://tasks-api.drolet.cloud/search \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query":"Disney","completed":null,"limit":25}'
```

**Why it matters:** the new email is the *resolution* of a thread that had
already been worked and closed twice. It is the good-news end of a story, not a
new obligation. Creating a third P1 task asks the reader to re-derive the whole
history before concluding there is nothing to do.

**Signal to look for:** a strong match against an existing task — especially a
**completed** one — on vendor + amount + instrument. Note the near-miss on
amount here ($24.98 vs $24.58): matching must tolerate small variance rather
than requiring an exact string hit. When a match lands, prefer a comment on the
existing task over a new one, and reopen only if the email shows the matter
regressed.

---

## Cost of getting this wrong

Both tasks landed as **P1** in `To Review`, which is the same shelf as things
that genuinely need doing. The cost is not the two minutes to close them — it is
that every unnecessary P1 lowers the trust that any given P1 is real, which is
the one property the whole pipeline depends on.

# No action needed

Tasks created from emails that require nothing of the recipient. Distinct from
[`more_context_needed.md`](more_context_needed.md): those needed evidence from
*outside* the email (prior mail, an existing task) to be recognized as moot.
These are recognizable as no-action from the **thread itself** — the pipeline had
everything it needed and still produced a task.

Two families show up here.

**Family A — acknowledgment replies.** Someone acknowledging a decision the user
already made and already communicated. The user is the originator of the thread;
the inbound message is a courtesy reply closing the loop.

**Family B — informational notices.** A service reporting a state change that
asks nothing: a statement is available, a payment series is ending, a product
policy will change. The sender is announcing, not requesting.

Both share the same tell: there is no request, no deadline that binds the user,
and no deliverable. Ignoring the message costs nothing.

## Signals — Family A (acknowledgment replies)

- **The user sent the parent message.** The thread root is `From: <the user>`.
  A reply to your own announcement is, by default, not a task. This is the
  strongest single signal and it is cheap to check — the quoted thread is in the
  body.
- **The message is declarative, not interrogative.** No question mark directed
  at the user, no ask, no attachment to review, no link to act on.
- **Acknowledgment vocabulary:** "thanks for letting us know", "ok, understood",
  "no problem", "best wishes", "sounds good", "hope everything is ok".
- **The verb the summarizer reaches for is hollow.** When the only title an
  enrichment pass can produce is `Acknowledge X` or `Review X reply`, that is
  usually a sign there is no action to name. Real tasks yield concrete verbs —
  *pay*, *book*, *return*, *reply to*, *submit*.
- **N replies → N tasks.** Several recipients answering one announcement produce
  several near-identical tasks. If the pipeline is about to create a task whose
  thread already produced one, that is a duplicate at minimum and usually a
  no-action at root.

---

## Family A examples

Both come from one thread. On 2026-08-14 15:42 UTC, Ben emailed Christy Dillon,
Andrew Clemens, and Patricia Cartes:

> Hi Christy, Andrew, and Patricia,
>
> I have made a hard decision not to coach this upcoming season. I have some
> things in my personal life that I need to focus my time and energy on. I do
> hope that you guys have a great season and enjoy your time with the kids.
>
> Looking forward to future seasons,
> Ben

The decision was made and delivered before either task existed. Two recipients
replied, and each reply became its own P1... classified `review`, P3 importance,
landing in `To Review`.

### `h3h` — Andrew Clemens's reply

**Task:** `[P3] Acknowledge Ben's coaching resignation`, gid `1217496996669132`,
created 2026-08-14 16:17.

**The message** (2026-08-14 16:16, to Ben, cc Christy + Patricia):

> Ok thanks, Ben. I'm sure there will be some other parents we can tap into when
> needed. Hope everything is ok!

**Why it is no-action:** Andrew accepts the decision and states how *he* will
handle the gap — recruiting other parents is his move, not Ben's. "Hope
everything is ok!" is a pleasantry, not a question requiring an answer.

**What the enrichment made of it:** the key points included "Andrew suggests
recruiting other parents to fill the coaching gap" — a fact about someone else's
plan, elevated into something that reads like a to-do. This is the failure mode:
summarizing *the other party's* next step as though it were the user's.

### `pn0` — Christy Dillon's reply

**Task:** `[P3] Acknowledge Ben's coaching resignation`, gid `1217499335151233`,
created 2026-08-14 17:08.

**The message** (2026-08-14 17:07, to Andrew, cc Ben + Patricia):

> thank you for letting us know Ben, best wishes to you and see you next week.

**Why it is no-action:** pure acknowledgment. Note also that Ben was **cc**, not
a direct recipient — Christy replied to Andrew. Being cc'd on a thread you
started, where the message is a thank-you, is about as clear a no-action signal
as exists.

**Duplicate on top of no-action:** this task is titled identically to `h3h` and
was created 51 minutes later from the same thread. Even if a reply to your own
announcement warranted tracking, it warrants *one* item, not one per responder.

---

## Signals — Family B (informational notices)

- **The generated summary contains its own disqualifier.** "no action required",
  "no action is needed", "for your records", "this is a confirmation". When the
  enrichment writes that phrase into a key point, that is a hard stop, not a
  caveat to file alongside a P1.
- **The described action belongs to the sender.** "Final payment *will be sent*",
  "your statement *is now available*", "this feature *will auto-enable*". The
  subject of the verb is the service, not the user.
- **The date is the sender's schedule, not the user's obligation.** A series that
  ends on the 17th and a statement posted on the 15th are calendar facts. They
  look like due dates and are not.
- **Availability language:** "is now available", "is ready to view", "you can
  now", "here are the details".
- **Broadcast, not addressed.** Vendor policy announcements go to every admin or
  every customer. Nothing in the message is specific to this user.
- **Recurring cadence.** If the same notice arrives monthly, it is a feed, not a
  task. Twelve identical tasks a year is the signature of a misfiled subscription.

## Family B examples

### `paf` — Zelle series ending

**Task:** `[P1] Review ending Zelle payment schedule`, gid `1217526444975656`,
created 2026-08-17, due 2026-09-17.

**The generated key points, verbatim:**

> Repeating monthly Zelle payments of $250 to Robert Drolet are ending on Sep 17,
> 2026 after 8 total payments
>
> Final payment will be sent on the scheduled date; **no action required** unless
> you want to modify or cancel

**Why it is no-action:** the pipeline wrote "no action required" into the task and
created the task anyway, at P1. This is the cleanest possible failure — the
disqualifying evidence is not in a prior email or a sibling task, it is in the
enrichment's own output, two lines below the title that says "Review".

**The residue:** a real decision does exist — whether $250/mo to Robert Drolet
should continue past payment 8 — but "review this notice" is not that decision,
and its natural trigger is September, not the day the notice arrived. A decision
the user must make is a different artifact from a notice the user received.

**Rule:** if a generated key point contains an explicit no-action phrase, do not
create a task. Reclassify as reference.

### `ab9` — Google Meet automatic note-taking

**Task:** `[P1] Review Google Meet automatic note-taking settings`, gid
`1217556780396026`, created 2026-08-17, due 2026-09-21.

**The source:** a Google Workspace announcement that "Take notes for me" will
auto-enable for meetings with 3+ guests starting 2026-09-21, with opt-out
available org-wide in the Admin console and per-meeting for organizers and guests.

**Why it is filed here:** the email is a **broadcast vendor announcement** sent to
all Workspace admins — nothing in it is addressed to this user or conditioned on
anything this user did. More pointedly, the third generated key point reads:

> **Action required:** Review Automatic note-taking settings in Google Admin
> console before September 21 and opt out organization-wide if needed

Google did not say "action required." The enrichment synthesized that phrase and
then honored its own invention by filing a P1. A pipeline that manufactures
urgency and then responds to it will fill the queue indefinitely.

**Reservation, recorded honestly:** this is the weakest member of the set, and I
argued against including it. The underlying change is real and has a genuine
confidentiality dimension — automatic AI transcription switching itself on for
mediation calls is not a trivial default. Filed here at Ben's direction. The
defensible reading is that `ab9`'s problem is *framing and timing* rather than
the absence of any action: a five-weeks-out settings change entered the queue as
a P1 "review" on the day the email landed. If a third bucket is ever added to
these docs — *real action, wrong priority, wrong date* — this example likely
moves there.

**Rule:** never synthesize "Action required" into a key point. If the source does
not ask, the task must not either. Broadcast announcements default to reference;
when a change genuinely warrants a decision, the task belongs near the date, at
the priority the decision earns.

### `tid` — PayPal monthly statement

**Task:** `[P2] Review July PayPal account statement`, gid `1217513863157684`,
created 2026-08-15, no due date.

**The generated key points:**

> July account statement is now available for review in PayPal account
>
> Log in to account and select monthly statement to view details
>
> PayPal app available for tracking account activity and earning rewards

**Why it is no-action:** no amount, no discrepancy, no deadline, no request — a
pure availability notice on a monthly cadence. Note also that the third key point
is **marketing copy** ("earning rewards") promoted to the status of a fact worth
recording, which is its own small failure.

**Rule:** recurring availability notices are reference, permanently. If the user
reconciles statements monthly, that is a standing habit — one recurring task or
none — not twelve inbox items a year. And promotional sentences must never be
extracted as key points.

## What good handling looks like

For a thread the user originated where replies are acknowledgments:

1. **Create nothing.** Classify `reference` or `ignore` and let it sit in mail.
2. If something genuinely open remains — here, **Patricia Cartes never replied**
   — that is the only candidate for a task, and it belongs to the *thread*, not
   to each inbound message.
3. If a task already exists for the thread, subsequent replies become **comments
   on it**, never new tasks.

For an informational notice:

1. **Classify `reference` and stop.** The mail is the record; a task adds nothing.
2. **Check the enrichment's own output before creating.** An explicit no-action
   phrase in a generated key point is a veto, and it is free to test for.
3. **Never invent an imperative.** "Action required" must appear in the source or
   not at all.
4. If a genuine decision hides inside a notice, create *that decision* — named as
   a decision, dated when it must be made, priced at what it is actually worth —
   not a "review" of the notice that revealed it.

## Cost

`h3h` and `pn0` sat open in `To Review` from 2026-08-14 to 2026-08-17. Neither
could be dispatched without opening the thread, reading three messages, and
concluding that nothing was ever owed. That reading cost is the real damage: a
task whose only possible resolution is "oh — nothing" trains the reader to open
tasks skeptically, which is exactly the habit the queue cannot afford.

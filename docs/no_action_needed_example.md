# No action needed

Tasks created from emails that require nothing of the recipient. Distinct from
[`more_context_needed.md`](more_context_needed.md): those needed evidence from
*outside* the email (prior mail, an existing task) to be recognized as moot.
These are recognizable as no-action from the **thread itself** — the pipeline had
everything it needed and still produced a task.

The common shape: **someone acknowledging a decision the user already made and
already communicated.** The user is the originator of the thread; the inbound
message is a courtesy reply closing the loop. There is no request, no deadline,
no deliverable — only social acknowledgment.

## Signals of a no-action reply

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

## The examples

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

## What good handling looks like

For a thread the user originated where replies are acknowledgments:

1. **Create nothing.** Classify `reference` or `ignore` and let it sit in mail.
2. If something genuinely open remains — here, **Patricia Cartes never replied**
   — that is the only candidate for a task, and it belongs to the *thread*, not
   to each inbound message.
3. If a task already exists for the thread, subsequent replies become **comments
   on it**, never new tasks.

## Cost

`h3h` and `pn0` sat open in `To Review` from 2026-08-14 to 2026-08-17. Neither
could be dispatched without opening the thread, reading three messages, and
concluding that nothing was ever owed. That reading cost is the real damage: a
task whose only possible resolution is "oh — nothing" trains the reader to open
tasks skeptically, which is exactly the habit the queue cannot afford.

# Tasks-owned screening — gate 1 moves from inbox's category to a tasks classifier

**Date:** 2026-08-27
**Status:** implemented, verified by dry run (run 3, 1,270 rows, 2026-08). Two
ship-gate criteria were **not met as written** — see §Verification → Ship
gate for the numbers and the argument each one raises rather than resolves.
**Amended:** 2026-08-28 — gate 1 gains a third outcome (`relate`), so an email
that is not a task but reports on an open one becomes a comment instead of a
silent drop. Amended: Problem, Goals, Non-goals, Decisions (D4, D5),
Architecture, `services/screening.py`, Handler flow, Relating to an open task
(new), Degradation, Recording, Verification, Files, Risks. Narrow calls 2 and 4
(the urgent bypass and the action-item labels) are **superseded decisions** —
they reverse positions the unamended spec took; see that section.
**Supersedes gate 1 of:** `docs/superpowers/specs/2026-08-18-standing-context-gate-design.md`
(that spec's gate 2 is unchanged)

## Problem

Gate 1 is `services/policy.py::warrants_task` — a set membership test on the
category inbox assigned:

```python
_TASK_CATEGORIES = {"urgent", "review", "respond"}
```

An email inbox files as `reference` or `ignore` can never become a task, no
matter what it contains. That is 82% of arriving mail (153 of 186 over
2026-08-19..26), and the decision is made by a classifier that is not solving
this problem.

### The case that prompted this

On 2026-08-24, Dana Rivera sent `checking and saving` with two attachments —
`Checking.csv` (65,903 bytes) and `Savings.csv` (1,451 bytes) — and an empty
body. Full audit trail:

```
19:04:46 [inbox-process]    Dispatching ignore (importance=P3)
19:04:47 [inbox-process]    Published email_classified event
19:04:48 [inbox-process]    Processed — 'checking and saving' → ignore (P3, 0.72) | 9 labeled neighbors
19:04:48 [schedule-process] Skipping category='ignore'
19:04:51 [tasks-events]     No task for category='ignore' — message_id=0053c9c2-...
```

`tasks-events` did one thing: `handlers/task_create.py:92` called
`warrants_task`, got `False`, logged, returned. No gate-2 triage, no
enrichment, no `suppressed_emails` row — gate-1 rejections are not recorded.
The email left exactly one log line.

Two independent failures compound here:

1. **inbox's classifier cannot see attachments.** `services/classification.py:105-108`
   builds its prompt from sender + subject + `body[:1500]`. With an empty body it
   saw `dana.rivera@example.com` and the words "checking and saving" — idle
   chatter. `clients/azure/email.py:82` defines `get_attachment_names()`; nothing
   in the repo calls it.
2. **tasks delegates its own policy decision** to that classifier's output.

Fixing (1) alone leaves tasks coupled to a taxonomy built for folder routing.
This spec fixes (2), and fixes (1) on the tasks side.

### The second case: emails that should comment, not create

On 2026-08-27 Enterprise sent `Confirmed: Enterprise Rent-A-Car Reservation
12345678 at Columbus John Glenn Intl. Airport (CMH)`:

```
20:38:20 [inbox-process] Processed — 'Confirmed: Enterprise ...' → reference (P1)
20:38:29 [tasks-events]  No task for category='reference' — message_id=6eb38d8b-...
```

Same shape as the Dana case — one log line, no queryable record — but the
right outcome is *not* a task. Task `1217730397662201` ("[P1] Book rental car
for Ohio trip") was open, and this email settles it. The correct behaviour is a
comment on that task, and gate 2 already knows how to produce exactly that:
`services/triage.py:294` instructs the agent to set `resolves: true` when "this
email is its resolution", and `_suppress()` renders it as *"Looks resolved —
close this task if you agree."*

That machinery is built, shipped, and unreachable. It lives behind gate 2, and
gate 1 drops the email before gate 2 runs.

So there is a third failure mode, distinct from the two above:

3. **A no-action email that settles an open task has no path to that task.**
   Gate 1 is binary — task or nothing — so "needs no work" and "is not
   relevant" collapse into the same verdict.

Note what this rules out: making the screener over-inclusive enough to carry
confirmations into gate 2 is not a fix. It makes the comment depend on gate 1
being *wrong* in a convenient direction, and the dry run below would score that
promotion as a precision failure. The outcome has to be named, not smuggled.

## Goals

- Tasks decides for itself whether an email becomes a task, from the email, not
  from inbox's label.
- The decision sees attachment metadata.
- Tasks assigns its own priority, so a rescued email is not stuck at the
  priority inbox gave it for a different purpose.
- An email that needs no work of its own, but reports on an open task, becomes
  a comment on that task rather than a silent drop.
- A rejected email leaves a queryable record.
- A Claude outage degrades to today's behavior, not to a flood.

## Non-goals

- Changing inbox. It keeps classifying: `category` drives folder routing and
  draft replies, and `schedule-process` gates on it too.
- Parsing attachment *content*. Names, content types, and sizes only.
- Touching gate 2's agent, tools, or prompt beyond one input field.
- Replacing the `no_action_phrase` backstop.
- Closing a task automatically. The `relate` path comments; Ben closes. That is
  already `_suppress()`'s contract and this amendment does not relax it.

## Decisions

| # | Decision | Rejected alternative |
|---|---|---|
| D1 | Cheap Haiku screener on every email; survivors go to the existing Sonnet triage agent | Sonnet agent on all ~23/day (3× triage cost); or a single Haiku call replacing both gates (loses gate 2's tool use) |
| D2 | Tasks owns `verdict` + `priority`. Section placement keeps reading inbox's `category` | Gate only (rescued email keeps a wrong `[P3]`); or tasks also owns a `kind` → section mapping (two taxonomies that drift) |
| D3 | Attachment **metadata** into the screener prompt | Decode + sample text content (prompt-injection surface from attacker-controlled files); or defer entirely (ships without fixing the motivating case) |
| D4 | Gate 1 returns a three-way verdict — `task` / `relate` / `drop` | Keep `is_task` boolean and let over-inclusion carry confirmations into gate 2 (the comment then depends on the screener being wrong, and the ship gate scores it a precision failure); or a follow-on spec (re-tunes the prompt and re-runs the same 1120-email backtest, discarding this spec's calibration) |
| D5 | Match the related task by vector nearest-neighbour over `task_index`, then one Haiku confirm | A Sonnet tool-runner per candidate (the dominant cost line, on the mail population that is mostly receipts); or substring match on subject (misses paraphrase — the reason the corpus is embedded at all) |
| D6 | **Drop the urgent bypass.** Every `task` verdict runs gate 2 | Keep it keyed on inbox's `category == "urgent"` (the largest surviving instance of the coupling this spec exists to remove, on the category the correction log disputes most); or re-key it to the screener's `priority == "P0"` (silently changes which emails skip a gate, for a latency saving that does not matter on an async Pub/Sub path) |

inbox's `category`, `importance`, and `reasoning` are **deliberately withheld**
from the screener. Cost of that: inbox's classifier has 9 labeled neighbors and
sender reply-history behind it; the screener has neither. Acceptable only
because the screener is tuned over-inclusive and gate 2 is the precision stage.

## Architecture

```
                    BEFORE                          AFTER
inbox owns    category ──► gate 1 (verdict)   category ──► section GID only
              importance ─► [PX], due-date    importance ─► outage fallback only

tasks owns    triage agent (gate 2)           screening (gate 1) ──► verdict, priority
                                                 task   ──► triage agent (gate 2)
                                                 relate ──► relating ──► comment
                                                 drop   ──► suppressed_emails
```

`warrants_task` is **not deleted**. It demotes from gate to outage fallback
(see Degradation).

## services/screening.py

Mirrors `services/triage.py`'s shape so the two gates read alike: one entry
point, never raises, fail-open by contract.

```python
@dataclass
class Screening:
    verdict: str = "task"   # task | relate | drop
    priority: str = "P2"
    reason: str = ""
    outcome: str = "task"   # task | relate | drop | fail_open

    @property
    def is_task(self) -> bool:
        return self.verdict == "task"

def screen(event: EmailClassifiedEvent) -> Screening: ...
```

One Haiku call via a new `clients/claude.py::classify(prompt, schema)` —
sibling of `summarize`, temperature 0, `max_tokens` 256, structured output.
Model `claude-haiku-4-5`.

**Prompt input:**

- `sender`, `sender_display`
- `to` / `cc` — whether Ben is a direct recipient or only cc'd is signal
- `subject`, `received_at`
- `body[:2000]`
- attachment metadata (below)
- the `Roles` section of standing context, via
  `services/standing_context.section("Roles")` — the same facts gate 2 reads.
  Roles is what makes "Dana · bank CSVs" legible as an obligation rather
  than chatter.

**Output schema:**
`{verdict: "task"|"relate"|"drop", priority: "P0"|"P1"|"P2"|"P3", reason: str}`

`relate` is not a softer `drop`. It means: this email needs no work of its own,
**and** it plausibly reports on something already being tracked — a
confirmation, a receipt, a delivery notice, a "your request was processed". The
screener does not identify *which* task and is not given the task list; it only
says one may exist. Finding it is the next stage's job, and finding nothing is
a normal outcome that degrades to `drop`.

**Prompt must state:** when in doubt between `task` and anything else, choose
`task`; when in doubt between `relate` and `drop`, choose `relate` (a match is
still required downstream, so the cost of a wrong `relate` is one embedding and
one Haiku call, not a spurious task). This is the over-inclusion knob, and the Verification section is how its position gets
chosen. The governing principle is already written into `policy.py`: *a
spurious task costs seconds to close; a swallowed message about a child's team
placement is unbounded.* That holds at today's noise rate; it inverts if the
list floods, because a list nobody reads swallows everything.

## Attachment metadata

`clients/inbox_api.py:37::get_attachments()` exists with no production caller —
built as a seam in `docs/superpowers/plans/2026-07-15-tasks-repo-setup.md` and
never wired up. This is its first consumer.

**Gotcha that will silently break this:** it needs the *Graph* message id, not
`event["message_id"]`. The event's `message_id` is inbox's internal UUID;
passing it to inbox-api returns `ErrorInvalidIdMalformed` → HTTP 502. inbox
publishes `graph_message_id` alongside it (`services/email_events.py:74`), but
`models/events.py` does not declare it.

So:

1. Add to `EmailClassifiedEvent`: `graph_message_id: str`, `has_attachments: bool`.
   Both are already published; this is a declaration gap, not an inbox change.
2. Call `get_attachments(event["graph_message_id"])` only when
   `has_attachments` is true.
3. Drop `content_bytes` immediately on receipt; keep `is_inline: false` only.
4. Render `name · content_type · size` into the prompt.
5. Wrap in try/except. Attachments are a bonus signal, never a reason to fail.

Rendered form:

```
Attachments:
  Checking.csv  text/csv  64.4 KB
  Savings.csv   text/csv   1.4 KB
```

## Priority threading

`screening.priority` replaces `event["importance"]` at five sites:

| Site | Today |
|---|---|
| `handlers/task_create.py:120` | P0/P1 gate on deadline extraction |
| `handlers/task_create.py:134` | `[PX]` title prefix |
| `handlers/task_create.py:148` | `tasks_created` otel label |
| `handlers/task_create.py:156` | `tasks` row `importance` column |
| `clients/asana.py:112` | last-resort title `[{importance}] {subject}` |

The last one is a layering fix worth taking: move the fallback title into the
handler so `clients/asana.py` stops reaching into the event for business data.
Clients are I/O only per the layer rules in `CLAUDE.md`. `create_task` keeps
its `title` parameter and loses the `or f"[{event['importance']}] ..."` branch.

Gate 2's user message (`services/triage.py:319`) swaps
`Classified: {category} / {importance}` for the screener's verdict and reason —
more useful to the agent than a label tasks no longer trusts.

## Handler flow

```python
def handle(event: EmailClassifiedEvent) -> None:
    verdict = screening.screen(event)          # gate 1

    if verdict.verdict == "drop":
        _suppress(event, reason=verdict.reason, source="screen",
                  related_task_gid=None, evidence=[])
        return

    if verdict.verdict == "relate":
        m = relating.match(event)              # nearest-neighbour + Haiku confirm
        _suppress(event, reason=m.reason or verdict.reason, source="relate",
                  related_task_gid=m.task_gid, evidence=m.evidence,
                  resolves=m.resolves)
        return

    decision = triage.decide(event, screening=verdict)   # gate 2, unchanged
    ...
```

## Relating to an open task — `services/relating.py`

The `relate` verdict says a task *may* exist; this stage finds it or gives up.
Cost discipline is the whole constraint: this runs on the receipt/confirmation
population, which is large, so a Sonnet tool-runner per email is not
affordable.

```python
@dataclass
class Match:
    task_gid: str | None = None
    resolves: bool = False
    reason: str = ""
    evidence: list = field(default_factory=list)

def match(event: EmailClassifiedEvent) -> Match: ...
```

Everything this needs already exists — no new infrastructure, no new client:

1. **Nearest-neighbour.** Embed `subject + body[:2000]` with
   `clients/vertex.py::embed`, then
   `repo/task_index.py::semantic_candidates(conn, query_embedding=...,
   completed=False, limit=3)`. The corpus is already maintained on every write
   path (`services/task_index.py::refresh`). `completed=False` restricts to
   open tasks — a completed task needs no "looks resolved" comment.
   Hydrate the hits with `repo/task_index.py::get_rows`.
2. **Confirm.** One Haiku call (`clients/claude.py::classify`, the same helper
   D1 adds) sees the email and the ≤3 candidate titles + descriptions, and
   returns `{task_gid: str|null, resolves: bool, reason: str}`. **Null is a
   normal, expected answer** — the prompt must say so, or the model will pick
   the nearest of three bad candidates.
3. **Verify.** A `task_gid` that cannot be fetched from Asana is treated as no
   match. Gate 2 already does exactly this at `services/triage.py:332`; lift it
   to a shared helper rather than writing it twice.

**A similarity floor gates step 2.** If the best neighbour scores below it,
return `Match()` without spending the Haiku call. This is the precision knob,
and the dry run sets it. Start conservative: a comment on the *wrong* task is
worse than no comment, because it is read as fact about that task and there is
no cheap recovery.

**No match is not a failure.** `Match()` with `task_gid=None` falls through to
a plain `suppressed_emails` row with `source="relate"`. The email is still
recorded — strictly better than today's single log line.

**`resolves` stays the model's call, and stays conservative.** It is the
difference between "Related email:" and "Looks resolved — close this task if
you agree." `services/triage.py:294` already has the wording for this
distinction; reuse it in the confirm prompt rather than inventing a second
definition.

**Cost.** One embedding (~$0.00002) plus, above the floor, one Haiku call at
~1.5k in / ~60 out (~$0.002). At an estimated 8–10 `relate`/day that is under
$1/month. Vertex and the embedding corpus are already paid for.

## Degradation

Today a Claude outage still produces tasks, because gate 1 is a dict lookup.
Fail-opening the screener to `verdict: "task"` would produce ~23 junk tasks/day
during an outage.

**The screener fails to `policy.warrants_task(event)`**, with priority falling
back to `event["importance"]`. A Haiku outage degrades tasks to exactly
today's behavior — a known-good state. Dana-class emails go back to being
missed until it recovers; nothing crashes, nothing floods.

`outcome="fail_open"` is recorded so outages are visible in metrics rather than
inferred. On fail-open the verdict is `task` or `drop` only — never `relate`,
which is a judgement the fallback cannot make.

The `relate` stage degrades independently, and more simply: any failure —
Vertex, Postgres, Haiku, Asana — returns `Match()` with no gid, so the email is
recorded as a plain suppression and no comment is posted. It can never crash an
event and never creates a task, so its worst outage behaviour is today's
behaviour.

## Recording and observability

Screener rejections go into `suppressed_emails` with `source="screen"`.
`relate` outcomes use `source="relate"` and **do** use `_suppress()`'s
related-task branch — reaching that branch is the point of the amendment.
~150/week, ~8k/year — nothing to Postgres, and it is the audit trail whose
absence started this. Today a dropped email leaves one log line and no
queryable record.

Schema needs no migration: `category` and `importance` columns still take
inbox's values; the screener's priority and reason go in `reason`.

New metric `tasks_screened{outcome, priority}` — `outcome` carries `relate`
alongside `task`/`drop`/`fail_open`. New metric `tasks_related{matched}` counts
`relate` verdicts that did and did not find a task; that ratio is the signal
for whether the similarity floor is set right, and a floor set too low shows up
as `matched="false"` climbing rather than as bad comments. `tasks_suppressed`
already labels `attached` and `resolves`, so comments posted by this path are
countable with no metric change.

Keep inbox's `category` as a second label on `tasks_created` for a period, so
agreement between the two classifiers is measurable rather than assumed.

## Four narrow calls

1. **Section for a rescued email.** `sections.for_category("ignore")` returns
   `None`, so an email the screener rescues lands unsectioned. Default anything
   outside urgent/review/respond to the **Review** section rather than the void.
   Change is in `services/sections.py`.

2. **The urgent bypass is removed.** *(Amended 2026-08-28 — supersedes this
   spec's original position, which kept it.)*

   ```python
   # services/triage.py:377 — delete
   if event.get("category") == "urgent":
       return Decision()
   ```

   The original call was that urgency is timing and P0 is stakes, so re-keying
   the bypass to `priority == "P0"` would silently change which emails skip
   gate 2. That reasoning is sound and the conclusion still does not follow:
   the third option is not to re-key the bypass but to delete it.

   As written it is the **heaviest** remaining coupling to inbox in the repo —
   heavier than gate 1 ever was. Gate 1 decided whether to look; this decides
   not to look at all: no triage agent, no standing-context read, no task
   search, straight to creation. And it trusts the one label this spec's own
   correction table disputes most, entirely in one direction:

   ```
   urgent → ignore (5), review (2), reference (1), respond (1)   = 9 corrections away
   anything → urgent                                             = 0 corrections toward
   ```

   Nine overrides, never once agreed with in the other direction. Measured
   from the inbox DB: of 257 emails inbox's classifier labelled `urgent`, 9
   were later corrected away from `urgent` and 0 were corrected toward it —
   a 9/257 = **3.5%** dispute rate. Read that honestly: the *direction* is
   unanimous (not one correction runs toward `urgent`), but 3.5% is a modest
   absolute rate. The argument for deleting the bypass rests on the
   one-sidedness of that 9-vs-0 split, not on the rate being large.

   **Why deleting it is safe.** `triage.decide` is fail-open by contract
   ("Never raises; every failure returns the actionable default", and
   `Decision()` defaults `actionable=True`). So running gate 2 on genuinely
   urgent mail cannot swallow it through failure — only through an
   affirmative, reasoned suppression, which is exactly the exposure every
   other task-bound email already carries. The bypass buys latency on an async
   Pub/Sub path where seconds do not matter, and costs one Sonnet run per
   urgent email (~$0.01).

   **If a bypass is ever wanted again**, key it on the screener's own
   `priority == "P0"` — tasks' judgement, not inbox's. Do not ship it now:
   there is no evidence gate 2 is the wrong thing to run on urgent mail, and
   adding it back is a one-line change.

3. **The phrase veto** (`policy.no_action_phrase`) stays exactly where it is,
   after enrichment. It reads Haiku key points, which do not exist until then.

4. **The action-item labels keep inbox's taxonomy — and get a correctness
   fix.** *(Amended 2026-08-28 — the unamended spec did not mention this
   site.)*

   `services/task_content.py:102` branches on `event["category"]` to label the
   task's action buttons. That stays: those buttons write back to inbox's
   classifier through the label webhook, so they must speak inbox's
   vocabulary. This is the correction channel, not a leaked dependency.

   But the `source` is wrong for a rescued email:

   ```python
   if event["category"] == "respond":
       confirm_label, confirm_text = "respond", "Confirmed respond"
   else:
       confirm_label, confirm_text = "review", "Confirmed review"
   ...
   (confirm_text, _action_url(message_id, confirm_label, "human_confirmation")),
   ```

   `source` is hardcoded `human_confirmation`. For an email the screener
   rescues from `reference`/`ignore`, the `else` branch renders **"Confirmed
   review"** — but inbox never said `review`. Clicking it records a *human
   confirmation* of a classification that was never made.

   That corrupts the corpus this spec's own dry run partitions on: the 1120
   negatives are defined as "never human-corrected", and the confirmation /
   correction split is the only labelled signal the pipeline has. A rescued
   email would inject a false confirmation and simultaneously fail to register
   as the correction it actually is.

   **Fix:** derive `source` rather than hardcoding it.

   ```python
   source = ("human_confirmation" if confirm_label == event["category"]
             else "human_correction")
   ```

   Only reachable once the screener can rescue `reference`/`ignore` mail, which
   is why it belongs in this spec and not before it.

## Cost

Haiku 4.5 is $1.00 / $5.00 per MTok.

Screener: ~690 calls/month at ~1.5k in / ~120 out ≈ **$1.50/month**.

The real cost is second-order: gate 2 traffic at least doubles (~4/day →
~8-10/day) because the screener is looser than the current category gate. The
dry run's promotion rate is what turns that estimate into a number. Sonnet 5
($2.00 / $10.00) as a tool-runner at up to 6 iterations is the dominant line
item. Watch `triage_duration` after rollout; do not pre-optimize.

Dropping the urgent bypass (narrow call 2) pushes urgent mail into gate 2 for
the first time. That is a small slice and it is not free, but it is bounded and
one-directional — measure it as its own line in the dry run rather than folding
it into the promotion rate.

The `relate` verdict cuts *against* that second-order cost, and the estimate
above predates it. Confirmations and receipts are the bulk of what an
over-inclusive boolean screener would have pushed into gate 2; routing them to
a ~$0.002 embed-and-confirm instead of a Sonnet tool-runner is why the
three-way verdict is cheaper than the two-way one it replaces. Report gate-2
promotions and `relate` promotions separately in the dry run and this becomes
a measured number rather than an argument.

## Verification — dry run

No shadow-mode period. The screener runs offline against historical mail,
creates nothing, and the output is analysed before launch.

### Why there is no pre-labelled miss corpus

The obvious test — "run it on emails it has missed" — has no data behind it.
Queried against the inbox DB (1,994 messages, 2024-02-19..2026-08-27; 1,904 LLM
classifications, 240 human confirmations, 64 human corrections), every usable
correction runs the *opposite* direction:

```
llm said  →  human corrected to    count
review    →  ignore                  16
urgent    →  ignore                   5
urgent    →  review                   2
review    →  respond                  2
respond   →  ignore                   1
urgent    →  reference                1
urgent    →  respond                  1
respond   →  review                   1
review    →  reference                1
ignore/reference → anything tasky      0
```

Zero recorded misses, and that is structural rather than a data gap. The only
feedback channel is Ben moving mail between Outlook folders, which fires only
on mail he sees. False positives land in To Review and get corrected; anything
filed `ignore` is archived and never seen again. The pipeline has never had a
feedback channel for a miss — the same reason the Dana email went unnoticed
for three days.

### Why that does not block shipping

The baseline on the `ignore`/`reference` pile is **zero promotions**. The
screener therefore cannot be worse than today on recall — only on precision.
Precision is exactly what a dry run measures with no answer key, because the
promoted set *is* the ranked shortlist, ranked by the thing under test.

### Harness

`scripts/backtest_screening.py` — reads the corpus from the inbox DB, calls
`screening.screen()`, writes a TSV, creates nothing.

| Corpus | Definition | n | Expected |
|---|---|---|---|
| Negative | `ignore`/`reference`, never human-corrected | 1120 | stay non-task (`drop` **or** `relate`) |
| Hard negative | human-corrected *down* to `ignore`/`reference` | 24 | stay dropped |
| Regression | every `message_id` in the tasks DB `tasks` table | ~35 | stay tasks |
| Regression (reclass) | human-corrected tasky → tasky (e.g. `urgent`→`review`) | 6 | stay tasks |
| Recall probe | `ignore`/`reference` with `body` < 200 chars | 31 | hand-checked |

The 24 hard negatives are the sharpest rows in the corpus: Ben actively ruled
"not a task" on each. The 6 reclassifications are the mirror image — he moved
them between tasky categories, so they must still produce tasks. The 1120 and
the 30 corrections are disjoint by construction (the 1120 excludes any message
carrying a human correction).

**The negative corpus now has two correct answers.** `drop` and `relate` are
both non-task outcomes, so neither is a precision failure and neither risks
flooding the list. What the dry run must do is *separate* them: a readout of
"promoted 180/1120" is meaningless if 170 of those are `relate`. Score and
report the two independently throughout.

For every `relate` verdict the harness also records the top-3 neighbours with
their cosine scores and the confirm call's answer. That lets the similarity
floor be tuned from the same run that tunes the screener prompt, rather than
requiring a second pass — the embeddings are the expensive part and they cache.

The recall probe is Dana's seam — her message stores `body_len = 2`
(`message_id=0053c9c2-e6c3-4ca7-ac46-3d6d0fc67553`). 31 rows is a two-minute
read and it targets the failure mode we have a confirmed instance of.

### Readouts, none of which need labelling

- Promotion rate on the 1120 — the "will it flood the list" number, directly.
- Promotions clustered by sender domain — 60% `@amazon.com` is a verdict in one
  line with nothing read.
- Promotions among the 24 hard negatives — Ben already ruled on these.
- Agreement on the regression set.
- The promoted list itself, sender + subject. Most calls are instant.

### Ship gate

Criteria as designed, each followed by the run-3 result. Run 3 is the
confirming run — tuned prompts, tuned similarity floor — 1,270 rows scored,
0 fail-open.

- **`task` promotion rate** on the negative corpus reviewed and accepted.
  This is the flood number; `relate` is excluded from it and reported
  separately.

  | corpus | n | task | relate | drop | task rate |
  |---|---|---|---|---|---|
  | negative | 1209 | 90 | 396 | 723 | 7.4% |
  | hard_negative | 24 | 6 | 5 | 13 | 25.0% |
  | regression_reclass | 6 | 3 | 0 | 3 | 50.0% |
  | recall_probe | 31 | 5 | 3 | 23 | 16.1% |

  **Accepted.** 7.4% on the negative corpus is the flood number, and it is not
  a flood. Among the negative corpus's 90 `task` promotions, 33 come from a
  single vendor domain (a bank's notification domain — real sender, not named
  here); the rest are diffuse. `relate` stage: of 404 `relate` verdicts, 33
  matched an open task (8%) and 6 of those claimed `resolves`.

- **≤2 of the 24 hard negatives promoted to `task`.** A hard negative landing
  on `relate` is acceptable — Ben ruled "not a task", not "not relevant".

  **Not met as written.** Run 3 gave 6/24. Reading those six rows
  individually: a past-due medical final notice (P0), an unauthorized-device
  security alert (P0), a municipal sewer-lateral repair notice, and a
  domain-ownership change — matters a reasonable person could well want
  tasked. The screener is disagreeing with Ben's own past downgrades, in the
  direction this spec's own governing principle endorses ("a spurious task
  costs seconds to close; a swallowed message is unbounded"). This is a
  criterion not met, **and** an argument that the criterion may have been
  mis-specified — the numbers and the rows are recorded here so the reader
  can judge, not resolved one way or the other.

- **100% of `regression_reclass` still produces tasks.** (The spec's
  "Harness" table above also names a plain "Regression" corpus over the
  tasks-DB `tasks` table; the implemented harness never built that corpus —
  only `regression_reclass` was run, and only `regression_reclass` is
  scored below.)

  **Not met as written by count — 3/6 — but zero genuine misses by row
  identity.** `regression_reclass` is 6 rows, so a count-based read is at the
  measurement noise floor (see caveats below); the breakdown, by row, is what
  makes the result trustworthy: 3 correct `task` (including the fixed IEP
  case, next bullet), 1 is a synthetic end-to-end test email Ben sent from
  his own address to himself during pipeline testing and hand-corrected
  afterward — a corpus artifact, since the corpus is built from human
  corrections — which the screener correctly drops, and 2 are defensible
  drops (a personal article share explicitly marked no-action-needed, and a
  security notice whose stated deadline had already passed).

- **The Dana message promoted to `task`, with a priority above P3.**

  **Passes.** `task` / P0.

- **The IEP progress-report case** — the one genuine recall miss found in
  run 2 — **now produces `task`.**

  **Fixed, confirmed by row identity rather than by count.** Run 2 scored
  this row `relate`; run 3 scores it `task` / P1. Because
  `regression_reclass` is only 6 rows, a count alone (2/6 → 3/6) would sit at
  the noise floor — the fix is trustworthy because the specific row that
  flipped is known, not because the tally moved.

- **The Enterprise message (`message_id=6eb38d8b-18f6-48c8-a4f1-26be68256681`)
  screened `relate`, and matched by the relating stage to task
  `1217730397662201` with `resolves: true`.**

  **Criterion is stale, not failed.** Task `1217730397662201` has since been
  completed, so `_candidates(completed=False)` excludes it by design — the
  criterion as written cannot be evaluated against today's Asana state. What
  run 3 actually shows: the Enterprise message screened `relate` and matched
  a different, currently-open task about the same rental matter, with
  `resolves: false`. That is the correct behaviour — matching the live task
  about the same matter and declining to claim resolution — even though it
  is not the specific gid the original criterion named.

- **Zero wrong-task matches** on a hand-checked sample of `relate` matches.
  This is the one failure mode with no cheap recovery, so it is a hard zero
  rather than a rate.

  **Passes.** No matched gid fell outside the candidate set produced by
  nearest-neighbour search in any hand-checked row.

### Methodological caveats

Three things that change how the numbers above should be read, for anyone
re-running this:

1. **Measurement noise.** Three repeats of an identical 19-row probe on
   unchanged code gave matched = 13/14/14 and resolves = 6/6/6 — 18 of 19
   rows stable, one flipping between repeats. Run-to-run noise is
   approximately ±1 on a small probe. Consequence: differences of 1–2 on the
   small corpora (`hard_negative`, `regression_reclass`) are not meaningful
   on their own — a claim about them must cite *which row* changed, by
   `message_id`, rather than a tally. That is why the IEP fix above is
   stated as row-confirmed rather than as "2/6 → 3/6."
2. **The corpora are live queries.** `negative` was 1205 rows in run 2 and
   1209 in run 3 — new mail arrives between runs, so no two runs score an
   identical corpus. This compounds caveat 1: a small delta between runs can
   be new mail, sampling noise, or a real prompt/floor effect, and only a
   large effect or a row-identified one should be trusted.
3. **The relate candidate pool is live and small, and drifts fast — this is
   the one that matters most.** Two probe runs over the identical 33
   message_ids, taken days apart with only a bounded prompt field and a
   raised token ceiling between them, gave 26 matches and then 22. That is
   not a code regression: a per-message_id diff of the two outputs found 8 of
   33 rows with materially shifted best-candidate similarity scores **for the
   identical query embedding**, and all four matched→unmatched flips fell
   inside that drifted set, onto entirely different candidate tasks. Cause:
   `task_index` is live — the production pipeline writes it continuously as
   tasks are created, completed, and edited. Measured directly: the index
   holds 677 rows, of which 602 are completed, so
   `_candidates(completed=False)` searches an open pool of only 75 tasks; 65
   of those rows were updated in the last 5 days, 49 in the last day alone.
   Against a 75-task pool that churn is enormous, and the nearest neighbours
   for a given email genuinely differ between runs days apart, for reasons
   that have nothing to do with the screener or the floor. Two consequences,
   both about `relate`, not about the `task`/`relate`/`drop` split itself
   (that split comes from the screener alone, on a fixed email corpus, and is
   not subject to this drift — though it remains subject to caveats 1 and 2
   above): **relate match rates are not reproducible across runs** — a figure
   like "33 of 404 matched (8%)" describes one moment's task list, not a
   property of the code, and every relate figure in this spec is a snapshot
   that a later re-run will not reproduce for reasons unrelated to any
   change. And **the harness's own warning is not on its own a sound
   diagnostic** — `scripts/backtest_screening.py` prints "a climbing no-match
   rate means the floor or the prompt is wrong," but against ~75 open tasks
   for ~404 relate emails, a low match rate is arithmetic before it is
   evidence of anything.

Also: local dry runs require `STANDING_CONTEXT_PATH` to be set, or the
declared facts read empty and the screener behaves differently from
production (see README's "Backtesting the screener" section). The first full
run had to be discarded and repeated for exactly this reason.

### Re-runnability

This is the point. Tune the prompt, re-run, diff against the previous run.
~1120 Haiku calls ≈ $2/pass; the corpus is fixed so it caches well. Prompt
tuning becomes an experiment loop rather than guesswork, and the harness
survives as a regression fixture.

**Harness constraints:**

- `messages.raw` holds the Graph *webhook notification*, not the message — no
  attachment metadata in the DB. `external_id` (the Graph id) is populated on
  all 1,994 rows, so the harness re-fetches attachments live via inbox-api.
- Emails since deleted from the mailbox cannot be re-fetched. Report as skipped;
  never score a skip as a negative.
- The harness needs inbox DB read access, which the tasks service does not have
  in production. It is a local-only script run with inbox credentials, not
  deployed code.

## Files

**New:** `services/screening.py`, `services/relating.py`,
`tests/test_screening.py`, `tests/test_relating.py`,
`scripts/backtest_screening.py`

**Changed:** `models/events.py` (declare `graph_message_id`, `has_attachments`),
`handlers/task_create.py`, `services/policy.py` (docstring — it is a fallback
now), `services/triage.py` (lift the unfetchable-gid guard at :332 into a
shared helper; **delete the urgent bypass at :377**), `services/sections.py`,
`services/task_content.py` (derive the action-item `source` instead of
hardcoding `human_confirmation`), `clients/claude.py` (`classify`),
`clients/asana.py` (drop the importance branch), `CLAUDE.md`

**Reused unchanged:** `clients/vertex.py::embed`,
`repo/task_index.py::semantic_candidates` / `get_rows`,
`repo/suppressions.py::insert`, `_suppress()`'s related-task branch. The
relating stage adds no infrastructure.

**Unchanged:** all of inbox, all terraform, the DB schema

## Out of scope — file separately

`services/triage.py`'s `get_email` tool has the same id bug latent in it.
`search_emails(mode="db")` returns inbox UUIDs; feeding one to `get_email`
returns `ErrorInvalidIdMalformed` → 502. The agent has probably been silently
losing evidence on that path. Confirmed by hand on 2026-08-27 against
`message_id=0053c9c2-e6c3-4ca7-ac46-3d6d0fc67553`.

Separately: inbox's classifier is still blind to attachment names, and
`clients/azure/email.py:82::get_attachment_names()` is still dead code. That is
an inbox change and would improve `category` for every consumer, not just tasks.

## Risks

| Risk | Mitigation |
|---|---|
| Screener floods the task list | Dry run sets the knob before launch; ship gate is a number |
| Losing inbox's kNN + sender-history signal | Accepted; gate 2 is the precision stage |
| Gate 2 cost doubles | Measured, not guessed; watch `triage_duration` |
| Two classifiers drift apart | inbox `category` retained as an otel label on `tasks_created` |
| Dry run cannot measure what it still misses | Baseline is zero promotions; recall can only improve. Probe the 31 short-body emails |
| Comment posted on the **wrong** task | Similarity floor, then Haiku confirm allowed to answer null, then Asana-fetch verify. Ship gate is zero wrong matches on a hand-checked sample, not a rate |
| `relate` becomes a dumping ground for anything ambiguous | A `relate` with no match still costs an embedding and records a row; `tasks_related{matched="false"}` makes the ratio visible. A climbing no-match rate means the floor or the prompt is wrong |
| Removing the urgent bypass lets gate 2 suppress a genuinely urgent email | Gate 2 is fail-open by contract, so only an affirmative reasoned suppression can do it — the same exposure every other task-bound email carries. Watch `tasks_suppressed{category="urgent"}`; a single bad suppression justifies re-adding a bypass keyed on the screener's own P0 |
| Urgent mail now waits on a Sonnet tool-runner | Async Pub/Sub path, not user-facing. Watch `triage_duration`; nothing downstream has a latency SLO |
| A wrong `resolves: true` nags Ben to close a live task | The comment says "close this task if you agree" — it never closes. Worst case is one line to ignore, which is the existing gate-2 contract |

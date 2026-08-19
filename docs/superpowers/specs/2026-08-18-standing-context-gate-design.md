# Standing-Context Gate — Design

**Date:** 2026-08-18
**Revised:** 2026-08-19 — after review; gate 2 became a triage agent with
email and task search tools, bringing `docs/more_context_needed.md` and
`docs/no_action_needed_example.md` into scope
**Status:** Pending review

## Goal

Stop creating tasks that are correct on the email's own terms but moot given
something the pipeline could have known — a fact about Ben that lives nowhere
in the mail stream, prior mail from the same sender, or a task that already
exists in Asana.

Today `handlers/task_create.py` decides from one event in isolation:

```python
if not policy.warrants_task(event):   # category in {urgent, review, respond}
    return
```

That is the whole gate. This spec adds a **second gate**: a small triage agent
that is handed the email plus declared facts about Ben, can search and fetch
emails and tasks the way the `/searching-inbox-emails` and `/searching-tasks`
skills do by hand, and answers one question: *given everything I can find out,
does this email still require anything of him?*

It also introduces the declared-facts file the agent reads, and gives
`services/deadline.py` a second section of the same file to read.

## The evidence

Three families of false positive are already logged in this repo. The gate
must handle all three; the first is the one that motivated it.

### Standing context — the role changed, the mail did not

Ben resigned as Assistant Coach of the West Portal Proud Panthers on
2026-08-14. SF Microsoccer's admin mail continues, and he wants to keep
receiving it — he is still the parent of a player on that team. He does not
want it becoming action items.

Three Micro Admin emails have already produced three P1 tasks:

| Email | Task |
|---|---|
| 7/31 "Microsoccer Game Schedule Draft Ready for Review" | `[P1] Review microsoccer schedule draft by August 4` |
| 7/31 — *the same email* | `[P1] Review draft microsoccer game schedule` |
| 8/11 "Microsoccer Game Schedule Ready" | `[P1] Review microsoccer game schedule and register for meetings` |

**Those tasks were correct.** In July Ben was the assistant coach; a draft
schedule to review by Aug 4 was a real obligation with a real deadline. The
next identical email will be wrong. Nothing observable about the email changes
between the correct case and the wrong one — sender, subject, body, category
and importance are all the same. Only Ben changed.

This is the defining property of the problem and the reason it cannot be solved
by matching on the email.

### More context needed — the answer was in mail or in Asana

`docs/more_context_needed.md` logs two tasks that passed the policy gate and
were moot on arrival:

- **Xfinity bill** (`[P1] Review and pay Xfinity bill`, gid `1217530946487945`)
  — the mailbox held "Thanks for your payment — your automatic payment was
  processed" from 2026-08-10 and "Your automatic payment details" in the prior
  month's bill. One search for `from:xfinity` would have surfaced both; the
  triggering email itself carried the autopay block.
- **Disney Plus refund** (`[P1] Review Disney Plus refund confirmation`, gid
  `1217504339696293`) — two tasks for the same refund were already
  **completed** in Asana (`1217290596630525`, `1217290596663963`). One task
  search for `Disney` with `completed: null` would have found them; the email
  was the good-news end of a closed saga.

### No action needed — the thread itself was enough

`docs/no_action_needed_example.md` logs five tasks the thread alone
disqualified: two acknowledgment replies to Ben's own coaching-resignation
email (`h3h`, `pn0` — Ben was the thread root, one reply had him on cc), and
three informational notices (Zelle series ending, with "no action required"
written into the generated key points; a Google Meet broadcast where the
enrichment *invented* "Action required"; a monthly PayPal statement notice).

## Why matching cannot work

The obvious fix is a mute list keyed on sender. The mail says otherwise. Club
and league mail reaches Ben from at least six addresses across four domains:

| Sender | Carries | Actionable now? |
|---|---|---|
| `micro@sfvikings.com` | Micro Admin broadcasts | **No** — the target |
| `Lee@sfvikings.com` | "Elijah Drolet has been invited to join West Portal Proud Panthers" | **Yes** — parent-side |
| `noreply@sfvikings.org` | Account confirmations, player invites (note: `.org`) | Mixed |
| `noreply@fs18.formsite.com` | Coach registration results | Was, no longer |
| `khannegan@`, `aarongoose@`, `pauljthompson@`, `yalexander@` (gmail) | Practice cancelled, snack signups, game today | Mixed — personal addresses |
| `noreply@groups.google.com` | Team Google Group invitations | — |

Two failure modes, and the second is disqualifying:

1. **Under-inclusive.** Muting `micro@sfvikings.com` leaves the next blast from
   `Lee@` or a fresh `noreply@sfvikings.org` untouched. The address carrying
   the noise has already changed once.
2. **Over-inclusive.** `Lee@sfvikings.com` sends *both* admin traffic and
   Elijah's player invitations. A domain mute on `sfvikings.com` swallows mail
   about a child's team placement.

Subject matching fares no better: the team is renamed annually — Prowling
Panthers (SP25) → Speedy Panthers (2025-26) → Proud Panthers / "West Portal
Panthers" (2026).

The discriminator is Ben's **role**, which is not a string in the email.

The same argument applies to the other two families. A cosine threshold
against the task index gets the Disney case wrong in both directions ($24.98
vs $24.58; "Review refund confirmation" vs "Follow up on delayed refund"), and
no regex reads "Ben was cc'd on a thread he started." The evidence has to be
*looked up and judged*, not matched.

## Where the evidence lives

| Family | Missing evidence | Where it lives | How the gate gets it |
|---|---|---|---|
| Standing context | A fact about Ben's roles and relationships | **Only in Ben's head until declared** | `context/standing-context.md`, injected into the prompt |
| More context needed | Prior mail, or an existing Asana task | inbox-api, the task index, Asana | The agent's search/fetch tools |
| No action needed | Nothing — the thread itself is enough | In the email | Rules in the agent's system prompt, plus a deterministic phrase veto |

The first family is the only one that cannot be derived. It must be written
down. The other two are derivable — but only by asking the right question per
email, which is why gate 2 is an agent with tools rather than a fixed
retrieval step.

## The context file

**`context/standing-context.md`** — a new top-level directory, sectioned by
consumer.

```markdown
# Standing context

## Roles

### Assistant Coach, West Portal Proud Panthers (SF Microsoccer / SF Vikings)
**Ended 2026-08-14, for the 2026 fall season — that season runs 2026-09-12 to
2026-12-18.** Ben resigned; Christy Dillon is handling the replacement.
Elijah remains a player on the team.

- Coach- and admin-directed mail — schedules to review, coach admin
  requirements, Micro Admin broadcasts — is NOT actionable for this season.
- Mail about Elijah as a player — invitations, rosters, parent logistics —
  IS actionable.

## Calendar

- SFUSD 2026-27 fall term: 2026-08-17 to 2026-12-18.
- West Portal Elementary day: 7:50am-2:05pm Mon/Tue/Thu/Fri, 7:50am-12:50pm Wed.
```

**Why a directory and not `docs/`:** `terraform/cloud_functions.tf` excludes
`docs` from the Cloud Function source archive, so a file there would never
reach the running function. The archive's `excludes` is a denylist, so a new
top-level `context/` ships by default. This also keeps small runtime data out
of `docs/`, which holds plan documents in the hundreds of KB that have no
business in the function.

**Required companion change:** `.github/workflows/deploy.yml`'s `paths:` filter
is an allowlist. `context/**` must be added to it, or fact edits merge to
`main` and silently never deploy. This is a one-line, one-time change; it is
called out here because forgetting it produces a silent no-op on the one file
whose entire purpose is changing behavior.

**Why prose, not config:** the consumer is a model. Prose can express the
`Lee@sfvikings.com` distinction — same sender, opposite verdicts, decided by
what the mail is *about* — that no schema can encode. It is also the format
`docs/more_context_needed.md` and `docs/no_action_needed_example.md` already
use.

**Sections are addressed by heading.** `standing_context.section("Roles")`
returns that section's body, or `""` if absent. Each consumer reads only what
it needs: the triage agent reads `Roles`, deadline extraction reads
`Calendar`. This keeps per-call token cost proportional to relevance rather
than to the file's total size.

Retiring a fact is deleting its block. There is no expiry field to maintain and
no matcher to operate on.

### Facts carry their own scope

**A fact that applies only for a bounded period states that period in its own
prose**, as the coach block above does ("for the 2026 fall season — that season
runs 2026-09-12 to 2026-12-18"). The model is given today's date and decides
whether the fact still applies.

This is the primary defence against stale facts, and it is preferred over any
external expiry mechanism because it requires no machinery, no scheduled job,
and no human acting on a reminder. A fact whose window has closed simply stops
being applied, and the failure direction is over-creating tasks — the
survivable one.

**A fact with no stated end is perpetual.** That must be a deliberate choice by
the author, not an oversight: some facts genuinely have no season ("Ben is no
longer a customer of X"), and inventing an end date for them is worse than
stating none.

**This imposes a hard requirement on the agent prompt:** it must include
today's date. Scoped facts are uninterpretable without it.

## Design

### Gate 2 — the triage agent

```
policy.warrants_task(event)           gate 1 — category, free, unchanged
triage.decide(event)                  gate 2 — NEW: agent with read-only tools
email_summary.generate(event)         Haiku call — only if actionable
deadline.extract_deadline(event)      Sonnet call — only if actionable, P0/P1
asana.create_task(...)
```

Gate 2 runs **before** enrichment, so suppressed mail costs nothing beyond the
triage run itself, and the decision is made on the email plus whatever the
agent chose to look up — not on a summary of it.

**Runtime.** The agent is the Anthropic SDK tool runner
(`client.beta.messages.tool_runner`) running in-process in the `tasks-events`
Cloud Function, on **`claude-sonnet-5`** with adaptive thinking at
`effort: "medium"`. Not Managed Agents: the CF already holds every credential
the tools need (inbox-api token, Asana token, Cloud SQL, Vertex IAM), the tools
are thin wrappers over clients that already exist, and nothing needs a sandbox.
A hosted session plus a vault would add infrastructure to do what four
decorated Python functions do locally.

**Input.** One user message carrying: today's date; the `Roles` section of
`context/standing-context.md` (omitted when empty); and the email — subject,
sender, `to`, `cc`, received time, category/importance, and the body (quoted
thread included, capped as today at 3k chars). The system prompt is fixed
text (rules below) and is the first prompt-cache breakpoint; the tool
definitions sit in front of it in the rendered prompt, so one
`cache_control` on the system block caches both. At ~1.5k tokens it clears
Sonnet 5's 1024-token minimum.

**Tools — all read-only, all thin.** Each is a `@beta_tool` function in
`services/triage.py` delegating to an existing client; each catches its own
exceptions and returns an error result (`is_error: true`) rather than raising,
so one backend outage degrades the agent's evidence rather than aborting the
run.

| Tool | Backs onto | Mirrors |
|---|---|---|
| `search_emails(query, mode="graph", limit=10)` | `clients/inbox_api.search` — **new**, `POST /search` on inbox-api; `graph` mode takes KQL (`from:micro@sfvikings.com`, `subject:...`), `db` mode searches processed mail with classification | `/searching-inbox-emails` |
| `get_email(message_id)` | `clients/inbox_api.get_email` — exists | `/fetching-inbox-email` |
| `search_tasks(query, semantic=False, completed=None, limit=10)` | in-process: `clients/vertex.embed` + `repo/task_index.semantic_candidates` for semantic, substring over `task_index.title/notes` otherwise; `completed=None` searches open and done; a `repo/tasks` lookup by `message_id` is always included so "this email already has a task" is one call | `/searching-tasks` |
| `get_task(task_gid)` | `clients/asana.get_task_detail` + `get_stories` (description, due, completed, recent comments) | `/fetching-task` |

Tool descriptions say *when* to call them, not just what they do — e.g.
"call `search_emails` with `from:<sender>` when the mail describes a recurring
obligation (a bill, renewal, subscription) to check whether it is already
automated or paid". The tasks-api and inbox-api are not called through their
own HTTP surfaces from inside the CF: task search runs against the same index
and DB the API uses, which avoids a self-call and a second bearer token;
email search *must* go over HTTP because this repo never talks to Graph
directly (see `clients/inbox_api.py`).

**Rules in the system prompt** — in substance, the judgments the three docs
ask for:

- Set `actionable: false` ONLY when evidence you have (a standing fact that
  currently applies, prior mail, an existing task, or the thread itself)
  clearly shows the email requires nothing of Ben. If unsure, `true`.
- A standing fact that states a period applies only inside that period.
- A reply to a thread Ben started, or a message where Ben is only cc'd and
  the content is acknowledgment ("thanks for letting us know", "sounds
  good"), is not a task.
- "No action required", "for your records", "automatic payment", "will be
  charged automatically" in the email or in prior mail from the sender is
  disqualifying for a payment/review task.
- The sender's schedule (a statement posted, a series ending, a feature
  auto-enabling on a date) is not Ben's deadline. Broadcast vendor
  announcements default to not actionable. Never treat "action required" as
  present unless the source says it.
- If an existing task covers the same matter — same vendor/amount/instrument
  within small variance, same thread, same saga — set `related_task_gid` to
  it, open or completed. Only set it when you have fetched or seen enough of
  that task to be sure it is the same matter.
- Name every lookup you relied on in `evidence`.

**Output** — structured via `output_config.format` (JSON schema, strict):

```json
{
  "actionable": true,
  "reason": "one sentence naming the fact/email/task that decided it",
  "related_task_gid": null,
  "evidence": [{"kind": "email|task|fact", "ref": "message_id|gid|fact heading", "note": "..."}]
}
```

**Bounds.** `max_iterations` 6 (a run is typically 2–4 model turns); per-tool
HTTP timeout 10s; the whole `decide()` call runs under a hard wall-clock
deadline of 60s. `tasks-events` has a 120s timeout today; raising it to 300s
in terraform is recommended headroom, not a requirement. Expected volume is
~10–20 gate-1 emails/day at roughly 20–30k tokens per run — on the order of
$1/day on Sonnet 5.

### What happens with the decision

| Decision | Handler action |
|---|---|
| `actionable: true` | Enrich and create, exactly as today. |
| `actionable: false`, `related_task_gid: null` | Create nothing. Record the suppression (below). |
| `related_task_gid` set (either `actionable` value) | Create nothing. Post a comment on that task: `Related email: {subject} — {reason} — {web_link}` via `asana.create_story`. Record the suppression with the GID. **No reopen, no edits** — if the email shows the matter regressed the model should return `actionable: true` with no `related_task_gid`, and a new task is created; a comment is visible and cheap to undo, a wrong reopen is not. |

The comment is the one write this gate can cause, and it happens in the
handler after the decision — the agent itself has no write tools.

### Deterministic backstop — the no-action phrase veto

`no_action_needed_example.md` asks for a rule that is free to test: if the
enrichment's own key points contain an explicit no-action phrase, do not
create the task. That check stays, and runs on the Haiku summary *after* the
agent (the agent sees the email, not the summary):
`policy.no_action_phrase(key_points)` matches
`no action (is )?(required|needed)`, `for your records`, `automatic payment`,
`autopay` and suppresses with `source="phrase"`. It costs nothing and catches
the case where the agent judged wrong but the summarizer wrote the
disqualifier down anyway.

### Deadline context

`services/deadline.py` makes a Sonnet call for P0/P1 mail asking for an
explicit deadline. It is given today's date and nothing else, so relative
deadlines that depend on Ben's calendar ("before school starts", "by the end
of the fall session") resolve to `null`.

The `Calendar` section is injected into that existing prompt, ahead of the
current `Today is {today}` line. No new call, no new model, no change to the
return contract — still an ISO date or `null`.

Scope discipline: `Calendar` holds **dates and recurring schedules only**. It is
not a second place to describe roles, and the deadline prompt is not asked to
judge actionability. The two sections stay single-purpose so neither consumer
inherits the other's failure modes.

### Fail-open contract

The gate may only ever **remove** a task it is confident about. Every other
path creates the task:

- Agent run raises, times out (60s), hits `max_iterations` without a final
  answer, or returns `stop_reason: "refusal"` → create.
- Output fails schema validation, or `actionable: false` with an empty
  `reason` → create. A suppression that cannot name its justification is not
  trustworthy enough to act on.
- `related_task_gid` names a task that `get_task` cannot fetch → treat as no
  match; apply the `actionable` value alone.
- Any single tool failing → the agent sees `is_error`, continues with less
  evidence; the rule "if unsure, `true`" covers it.
- Missing or unreadable `context/standing-context.md` → the `Roles` block is
  simply absent; the agent still runs with its search tools.
- `event["category"] == "urgent"` → create, unconditionally — the agent is
  not even invoked.

The asymmetry is deliberate: a spurious task costs seconds to close; a
swallowed message about a child's team placement is unbounded.

**On the urgent exemption, precisely.** It does *not* make facts fresher or the
model more accurate. A stale or badly-worded fact is equally wrong for
`review` and `respond` mail, and that mail can matter. What the exemption does
is cap the blast radius of any gate error — model misjudgment, sloppy authoring,
or genuine decay — by keeping the highest-cost category out of reach. It is a
seatbelt, not a fix. It is nearly free because the mail this gate targets
classifies as `review`, so exempting `urgent` costs no coverage on the known
case. Reversible if it proves over-cautious.

### Staleness

A fact goes stale in four ways. Three are dangerous, one is not:

| Mode | Example | Direction |
|---|---|---|
| Role resumes, fact remains | Ben coaches again in 2027; "SafeSport cert expired, you cannot be on the field Saturday" is suppressed | **Dangerous** |
| Fact written too broadly | "SF Vikings mail is not actionable" eats Elijah's player invitation from `Lee@` | **Dangerous** (authoring error, not decay) |
| Role changes shape | Steps down as coach, becomes team manager — admin mail is signal again | **Dangerous** |
| Subject leaves entirely | Elijah leaves the team; player mail keeps creating tasks | Harmless — over-creates |

The dangerous direction is always the same: the fact asserts "not actionable"
and reality disagrees.

**Mode 1 is handled by scoping** (see "Facts carry their own scope"). A fact
that names the season it belongs to stops applying when that season ends, with
no human in the loop. This is the only mode with a clean structural fix, and it
is also the likeliest one, since roles are usually seasonal.

**Modes 2 and 3 have no structural fix and this spec does not claim one.**
Both are authoring errors rather than decay — mode 2 is wrong on the day it is
written, and mode 3 goes wrong *inside* the validity window, so no end date
catches it. Their backstops are:

1. **Fail-open.** A fact must clearly apply for the gate to act. Ambiguity
   creates the task.
2. **The suppression record.** "What has this fact eaten, and over what
   period" is a SQL query over `suppressed_emails.evidence` rather than an
   archaeological dig through logs — the only way an invisible failure
   becomes visible.
3. **Review at authoring time.** A fact lands via PR. The `Lee@sfvikings.com`
   distinction is exactly the thing a reviewer should be looking for, and this
   spec's sender table is the reason it is documented.

*Considered and rejected: a `Review by: YYYY-MM` field swept by the existing
daily `tasks-escalation` cron.* Scoping supersedes it for mode 1, and it does
nothing for modes 2 and 3 — it would add a scheduled job, a parser, and a
recurring notification to solve a problem the prose already solves.

The same staleness logic applies to the derived evidence, with one difference:
prior mail and existing tasks are re-fetched on every run, so they cannot go
stale the way a declared fact can. Their failure mode is mis-judgment, not
decay, and the `evidence` column is what makes that auditable.

### Recording suppressions

Log **and** database. No Asana artifact beyond the comment-on-existing case —
a P3 reference row would re-create the clutter this gate exists to remove.

New table in `repo/schema.sql`:

```sql
CREATE TABLE IF NOT EXISTS suppressed_emails (
    message_id       TEXT PRIMARY KEY,
    category         TEXT NOT NULL,
    importance       TEXT NOT NULL,
    subject          TEXT,
    sender           TEXT,
    reason           TEXT NOT NULL,    -- the model's reason, or the matched phrase
    source           TEXT NOT NULL,    -- 'agent' | 'phrase'
    related_task_gid TEXT,             -- set when the email was attached to an existing task
    evidence         JSONB,            -- the agent's evidence list, verbatim
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`schema.sql` is `CREATE TABLE IF NOT EXISTS` throughout and `scripts/migrate_db.py`
executes the whole file, so the migration is a re-run of the existing script.

Written through a new `repo/suppressions.py::insert`, following the existing
best-effort DB convention: a write failure logs at WARNING and does **not**
reverse the suppression. The decision was already made on evidence; undoing it
because Cloud SQL blipped would make task creation non-deterministic, which is
worse than a missing audit row. This is the one place the fail-open principle
deliberately does not apply, because the failure is in *recording* the
decision, not in *making* it. The same applies to the comment on an existing
task: if `create_story` fails, log and move on — the suppression stands.

Also emitted: counter `asana.tasks_suppressed` (exports as
`asana_tasks_suppressed`, matching the existing `asana_` series) with
attributes `category`, `importance`, `source`, and `attached` (`true` when a
`related_task_gid` was set). The reason and evidence are deliberately not
metric attributes — free text from a model would blow up cardinality. They
live in the log line and the table. Agent token usage flows through the
existing `claude_tokens` counter; a histogram `asana.triage.duration` and a
counter `asana.triage.tool_calls` (attribute `tool`) are added so cost and
latency of the gate are visible without reading logs.

### Layering

Per the repo's layer rules:

| File | Change | Concern |
|---|---|---|
| `context/standing-context.md` | **new** | The facts. Not code. |
| `services/standing_context.py` | **new** | Loads and caches the file; `section(name)` accessor. Returns `""` on any read failure. |
| `services/triage.py` | **new** | The agent: system prompt, the four `@beta_tool` functions (thin delegations), `decide(event) -> Decision`, the deadline/iteration bounds, fail-open parsing. Business logic lives here and nowhere else. |
| `services/policy.py` | `no_action_phrase(key_points)` added; `warrants_task` unchanged | Both gates' deterministic pieces in one file — CLAUDE.md: "Changing what becomes a task is a change HERE." |
| `clients/claude.py` | tool-runner entry point (`run_agent(...)`) alongside `summarize`/`extract`; records usage | I/O only. |
| `clients/inbox_api.py` | `search(query, *, mode, limit, mailboxes=None)` added | I/O only — first pipeline consumer of this seam. |
| `services/deadline.py` | prompt gains the `Calendar` section | Return contract unchanged. |
| `models/events.py` | `Decision` dataclass: `actionable: bool = True`, `reason: str = ""`, `related_task_gid: str \| None = None`, `evidence: list = []` | Pure type; the `True` default *is* the fail-open default. |
| `handlers/task_create.py` | gate 2 call before enrichment; suppression record; comment on related task; phrase veto after summary | Orchestration only. |
| `repo/suppressions.py` | **new** | `insert()`. Takes an open connection. |
| `repo/schema.sql` | `suppressed_emails` table | — |
| `clients/otel.py` | `tasks_suppressed`, `triage_duration`, `triage_tool_calls` | I/O only. |
| `.github/workflows/deploy.yml` | `context/**` in `paths:` | Without this, fact edits never deploy. |
| `terraform/cloud_functions.tf` | optional: `tasks-events` timeout 120 → 300 | Headroom. |

`services/email_summary.py` is **unchanged** — the earlier revision of this
spec put the actionability fields on the Haiku call; the agent replaces that.
No new dependency beyond the `anthropic` SDK already in `requirements.txt`
(the tool runner needs a current release; pin `>=0.116`).

### Testing

- `tests/test_standing_context.py` — section extraction by heading; missing
  file returns `""`; missing section returns `""`; caching.
- `tests/test_triage_tools.py` — each tool against mocked inbox-api / DB /
  Asana: happy path, and that a backend exception becomes an `is_error`
  result rather than a raise. `search_tasks` always includes the
  `message_id` match.
- `tests/test_triage.py` — `decide()` against a scripted fake client (a
  sequence of tool_use → tool_result → end_turn messages): a run that
  searches then suppresses; a run that finds a completed task and returns
  `related_task_gid`; and the fail-open table — exception, timeout,
  `max_iterations`, refusal, schema-invalid output, `false` with empty
  reason, unfetchable `related_task_gid`, urgent category short-circuit.
  The prompt must contain today's date and the `Roles` section when present.
- `tests/test_policy.py` — `no_action_phrase` truth table.
- `tests/test_deadline.py` — `Calendar` section reaches the prompt; empty
  section leaves the prompt unchanged.
- `tests/test_task_create.py` — a suppressing decision creates no Asana task,
  increments the counter, writes the row; a `related_task_gid` decision posts
  one story on that task and nothing else; a DB or story failure does not
  resurrect the task; an actionable decision proceeds to enrichment and
  creation unchanged; the phrase veto fires on a Haiku summary containing
  "no action required".

The Claude calls are mocked throughout via `monkeypatch.setattr(claude, ...)`,
as they are today.

Model judgment quality is verified manually against the real cases: the three
Micro Admin messages (must suppress, evidence = the coach fact), the two
`Lee@sfvikings.com` player invitations (must survive), Xfinity (must suppress,
evidence = prior payment mail), Disney (must attach to a completed task), the
two coaching-thread replies (must suppress, evidence = thread root), Zelle and
PayPal (must suppress, evidence = the email itself), Google Meet (must not
produce a P1 "Action required"). Scope expiry is verified by running the Micro
Admin emails against a frozen date after 2026-12-18 — they must **stop** being
suppressed.

## Out of scope

- **Duplicate suppression within a thread that has no task yet.** The 7/31
  email produced two near-identical tasks from one event (a Pub/Sub redelivery
  or a double publish). The existing `external:{message_id}` check in
  `create_task` is the right place for that and it is a distinct problem.
- **Reopening a completed task** when a related email shows the matter
  regressed. Today that yields a new task; reopen can be added once the
  comment path has a track record.
- **An API endpoint over `suppressed_emails`.** The table is queryable via
  `psql` for now. Add a route when there is a second reader.
- **Runtime editing of facts.** They change on the order of months. A PR is the
  right ceremony and gives the change a review and a history.
- **Letting the agent produce the summary and title too.** It could, in the
  same run, and that would save the Haiku call on actionable mail. Deferred:
  the summary path is tested and stable, and keeping decision and enrichment
  separate keeps each one's failure modes legible.

## Open questions

1. **How large can the context file grow before per-call cost matters?**
   Sectioning bounds this — each consumer pays only for its own section. At
   present size it is negligible. Revisit if any single section passes roughly
   1k tokens, at which point relevance-filtering within a section becomes worth
   designing.
2. **Should `Calendar` be consulted for P2/P3 mail?** Deadline extraction runs
   only for P0/P1 today. Widening it is a separate cost/benefit question this
   spec does not reopen.
3. **Should the agent run for `reference`-classified mail too?** Today gate 1
   drops it before the agent sees it. If the agent proves reliable, the
   inverse check — "this reference-classified mail actually needs a task" —
   is the same machinery pointed the other way. Not in this spec.

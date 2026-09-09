# Cross-project recurrence — per-project webhooks and project-aware completion

**Date:** 2026-09-08
**Status:** designed, not implemented
**Extends:** `docs/superpowers/specs/2026-09-03-recurring-tasks-design.md`

## Problem

A `repeat:` tag only works on a top-level task in the service's configured
project. Anywhere else it is added successfully and then silently never
fires. Three separate things cause this, and fixing any one alone changes
nothing:

1. **Delivery.** The Asana webhook is registered on exactly one resource —
   `ASANA_PROJECT_ID` (`scripts/register_webhook.py`). No completion event
   from any other project ever reaches the CF.
2. **Placement.** `services/recurrence.py::spawn_next` hardcodes
   `fields["projects"] = [asana.ASANA_PROJECT_ID]`. Even given the event,
   the successor would land in the default project rather than the one its
   predecessor lived in.
3. **Subtasks.** A subtask has no project membership at all, so both the
   `projects` field and section placement are meaningless for it.

The failure is silent, which is the worst property it could have. A user
adds `repeat:1y`, sees the tag applied, and learns a year later that nothing
happened. This was hit in practice on 2026-09-08: a Pacifica registration
renewal task was deliberately created in the default project *because* the
tag would not fire on Ben's Board, then moved to Ben's Board anyway, and the
tag had to be documented as inert in the task's own description.

## Goals

- `repeat:` fires for a task in any managed project.
- `repeat:` fires for a subtask, creating its successor under the same parent.
- Completion side effects (Done move, DB writes, index refresh, digest dirty
  flag) apply in every managed project, not only the default one.
- Adding a project to the managed set is a config change, not a code change.
- No flag day: the existing single webhook keeps working through the rollout.

## Non-goals

- **Workspace-wide automatic discovery.** Managed projects are configured
  explicitly. See D2.
- **Per-project section mapping beyond Done.** Review/Respond/Urgent stay
  default-project concepts; the pipeline only ever creates there.
- **Calendar-anchored recurrence, end conditions, subtask trees, time-of-day.**
  Unchanged non-goals from the 2026-09-03 spec.
- **Backfilling.** Tasks whose `repeat:` tag silently failed before this ships
  are not recovered.

## Decisions

### D1 — Per-project webhooks, because workspace webhooks cannot carry task events

The obvious design — one webhook on the workspace — does not work. Asana's
webhook documentation is explicit:

> To reduce the volume of data to transfer, webhooks created on `team`,
> `portfolio`, `goal` or `workspace` *must* specify filters. [...] Webhook
> events from tasks, subtasks, and stories won't be propagated to these
> higher-level webhooks, so all changes on these resources are automatically
> filtered out.

Workspace-level webhooks support only membership `added`/`removed`. Task
events are structurally unavailable at that level, so there is no
single-registration option. One webhook per managed project is the only
mechanism that delivers what this feature needs.

This drags in two problems the current single-webhook design does not have,
and D3–D5 exist to answer them: Asana mints a **distinct `X-Hook-Secret` per
webhook**, and webhooks are **deleted by Asana after 24 hours** of failed
delivery, so registration needs to be a repairable steady state rather than a
one-time runbook step.

### D2 — Managed projects are an explicit config map, not workspace discovery

A single terraform var → CF env var, `ASANA_MANAGED_PROJECTS`, holds a JSON
object keyed by project gid:

```json
{
  "<ben's board gid>": {"done": "<done section gid>"},
  "<family gid>":      {"done": "<done section gid>"},
  "<inbox gid>":       {"done": "<done section gid>"},
  "<mediation gid>":   {"done": null},
  "<cheryl gid>":      {"done": null}
}
```

Membership in this map *is* the definition of "managed": a webhook is
registered for it, its completions are handled, and its Done move happens
when `done` is non-null. One place to add a project.

Discovery was rejected. The workspace has five projects and they are stable,
so enumeration buys nothing and costs control: it would register webhooks on
projects as they appear, including ones created for a purpose that has
nothing to do with this service, and route their contents into the semantic
index and the due-day digest without anyone deciding that. An explicit map
makes that an act rather than an accident.

Project gids and section gids are personal and belong in `terraform.tfvars`,
never in this repo — same rule as the existing `ASANA_PROJECT_ID` and section
GIDs.

**All five current projects are managed**, including Mediation and Cheryl.
This was raised explicitly as a privacy consideration — managing a project
means every task event in it flows into the DB, the `task_index` semantic
corpus, and the due-day digest — and confirmed as intended.

### D3 — Secrets live in Postgres, keyed by project

Asana generates one `X-Hook-Secret` per webhook and it cannot be supplied by
the caller, so N managed projects means N secrets. They go in a new table:

```sql
CREATE TABLE asana_webhooks (
    project_gid   text PRIMARY KEY,
    webhook_gid   text,
    secret        text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT now()
);
```

**Secret Manager was rejected.** The alternative was one SM secret holding a
project→secret map, written by the reconciler, keeping the auth path off
Postgres. Three reasons it loses:

1. **Security posture.** The webhook CF is publicly invokable and
   unauthenticated by necessity — Asana posts to it without credentials.
   Secret Manager storage would require granting that identity
   `secretmanager.versions.add`. Giving a public endpoint write access to
   secret material is a materially worse trade than giving it another table
   in a database it already writes to.
2. **Ownership model.** `terraform/secrets.tf` owns every secret in this
   service. Application code minting secret versions at runtime creates
   secret material Terraform does not know about and cannot reconcile.
3. **Concurrency.** A single JSON map is read-modify-write; a reconciler run
   and a handshake landing together clobber each other. A table gets per-row
   writes and a natural primary key.

The availability argument — that SM is more available than Cloud SQL — is
real but does not decide it. Asana retries with exponential backoff, so a
transient outage costs delay rather than events (D6), and neither dependency
is plausibly down for the 24 hours it would take to lose a webhook.

### D4 — The target URL carries the project gid

Registration is `POST /webhooks` with
`target = <webhook-cf-url>?project=<project gid>`.

The handshake is **synchronous inside that call**: Asana calls the target with
`X-Hook-Secret` and waits for the echo before returning the webhook gid to the
caller. So at handshake time the CF does not yet know the webhook gid — the
project gid in the query string is the only key available, and is what the
secret row is written against. The reconciler writes `webhook_gid` back after
the POST returns.

The same query parameter tells `receive()` which secret to validate a later
delivery against.

### D5 — Reconciliation is a scheduled job, not a runbook step

`POST /webhook-sync` on the webhook CF, authenticated with the existing
`ASANA_ESCALATE_TOKEN` bearer (same reasoning as `/escalate` and `/digest`:
IAM cannot restrict a route on a function that must stay publicly invokable).
Driven by a new Cloud Scheduler job `tasks-webhook-sync`, daily.

Each run:

1. Read managed project gids from config.
2. `GET /webhooks?workspace=<ws>` and keep those whose target matches this
   service's URL.
3. Register a webhook for every managed project without an active one.
4. Delete every webhook of ours whose project is no longer managed, and drop
   its row.
5. Emit counts; log the diff.

A scheduled reconciler rather than a one-time script is what makes the
24-hour deletion behavior survivable: a webhook Asana drops during an outage
is re-registered on the next tick without anyone noticing it was gone.

### D6 — Failure modes, and why rejecting is safe

Asana's retry policy is the reason this design can afford to fail closed:

> if your servers are not available for an hour, you can expect it to take no
> longer than approximately an hour after they come back before the paused
> delivery resumes

Webhooks are deleted only after **24 hours** of failed delivery.

| Condition | Behavior |
|---|---|
| No `project` param on delivery | Fall back to `ASANA_WEBHOOK_SECRET` env (D7) |
| Unknown project / no secret row | 401; reconciler re-registers on next tick |
| DB unreachable, secret cached | Served from cache; delivery succeeds |
| DB unreachable, cache miss | 401; Asana retries, ~1h delay, no event loss |
| Asana deleted the webhook (24h) | Next reconciler tick re-registers, new secret |
| Registration call fails | Logged, retried next tick; no partial row |

Secrets are cached in a module-level dict with a 10-minute TTL, so steady
state does no database read on the delivery path. This keeps the DB
dependency off the hot path without making correctness depend on the cache.

**This is a deliberate, narrow exception to the layer rule that a DB outage
must never break event handling.** It is acceptable only because rejection is
non-destructive here: Asana redelivers. The rule still holds everywhere else
in the webhook path — index refresh and digest flagging remain best-effort and
must not fail a delivery.

### D7 — Rollout keeps the existing webhook working

`handlers/asana_webhook.py::receive` gains a `project_gid` parameter. When it
is absent, signature validation falls back to today's `ASANA_WEBHOOK_SECRET`
env var. The webhook currently registered on Ben's Board has no `project`
query parameter in its target, so it keeps validating unchanged while the new
path is deployed and populated.

Once the reconciler owns Ben's Board too, the legacy webhook is deleted and
the env fallback removed in a follow-up change. Nothing needs to happen
atomically.

### D8 — Done section resolution becomes project-aware

`services/sections.py::done()` currently returns one env var. It becomes
`done(project_gid: str | None)`:

- project gid present in `ASANA_MANAGED_PROJECTS` with a non-null `done` →
  that section gid
- project is the default project and the map has no entry → fall back to
  `ASANA_SECTION_DONE_GID`, preserving current behavior exactly
- otherwise → `None`, meaning **skip the move and log**, which is already the
  documented behavior when `ASANA_SECTION_DONE_GID` is unset

A completed **subtask** gets no Done move at all: it has no project
membership, so there is no Done section it could belong to.

### D9 — Successor placement follows the source task

`spawn_next` drops the hardcoded `ASANA_PROJECT_ID` and branches:

| Source task | Successor fields |
|---|---|
| Has `parent` (is a subtask) | `parent` = same parent; no `projects`, no section |
| Top-level | `projects` = the source's own memberships; section as today |

`asana.get_task_detail` must return `parent` and `memberships` in its
`opt_fields`; both are additive.

The existing `external.gid = recur:<completed gid>` idempotency guard is
unchanged and still does its job across projects — it is a workspace-wide
lookup, not a project-scoped one.

## Components

| Layer | File | Responsibility |
|---|---|---|
| `repo/` | `asana_webhooks.py` (new) | upsert secret by project, read one, list all, delete |
| `services/` | `webhook_registry.py` (new) | pure diff: managed set vs registered set → (to_register, to_delete) |
| `services/` | `sections.py` | `done(project_gid)` resolution (D8) |
| `services/` | `recurrence.py` | successor placement (D9) |
| `handlers/` | `webhook_sync.py` (new) | orchestrate the diff against the Asana API |
| `handlers/` | `asana_webhook.py` | handshake persists secret; `receive` validates per project |
| `handlers/` | `task_complete.py` | pass the task's project to `sections.done()` |
| `clients/` | `asana.py` | `list_webhooks`, `create_webhook`, `delete_webhook`; extra `opt_fields` |
| `main.py` | — | route `POST /webhook-sync`; pass `project` query param to `receive` |
| `terraform/` | `cloud_functions.tf`, `scheduler.tf` | `ASANA_MANAGED_PROJECTS` env; `tasks-webhook-sync` job |
| `repo/schema.sql` | — | `asana_webhooks` table |

The registry/handler split follows the existing layer rules: the diff is a
pure function over two sets and is unit-testable without Asana or a database;
the handler does the I/O.

## Observability

New metrics, `asana_`-prefixed per the existing convention:

- `asana.webhooks.registered` / `asana.webhooks.deleted` — counters from the
  reconciler
- `asana.webhooks.active` — gauge, managed projects with a live webhook
- `asana.webhook.auth_failures` — counter, labeled by reason
  (`unknown_project`, `no_secret`, `bad_signature`)

`asana.webhooks.active` falling below the managed-project count is the alert
that matters: it means deliveries are being dropped somewhere.

## Testing

**Pure units, no I/O:**
- `webhook_registry` diff — empty state, steady state, project added, project
  removed, webhook present for an unmanaged project
- `sections.done()` — mapped project, default-project fallback, unmapped
  project returns None
- successor field-building — subtask branch carries `parent` and omits
  `projects`; top-level branch carries source memberships

**Signature validation:**
- per-project secret validates; wrong project's secret rejects
- cache hit avoids the DB read; TTL expiry re-reads
- missing `project` param falls back to the env secret (D7)
- DB unavailable with a cold cache returns 401 rather than raising

**Recurrence, extending the existing suite:**
- successor lands in a non-default project
- subtask successor lands under the same parent, unsectioned
- `external.gid` guard still suppresses duplicates across projects

**Integration:** `scripts/` smoke that registers against a scratch project,
asserts the secret row, and deletes it.

## Rollout

1. Migrate the `asana_webhooks` table (`scripts/migrate_db.py`).
2. Deploy the code. The env fallback (D7) keeps the existing webhook working;
   nothing changes behaviorally yet.
3. Populate `ASANA_MANAGED_PROJECTS` in `terraform.tfvars` and apply.
4. Trigger `POST /webhook-sync` once by hand; verify one row per managed
   project and `asana.webhooks.active` equal to the managed count.
5. Complete a `repeat:` task in a non-default project and in a subtask;
   verify successors.
6. Follow-up change: delete the legacy Ben's Board webhook, let the reconciler
   register it, remove the env fallback and `scripts/register_webhook.py`.

Steps 1–2 and 3–4 are independently revertible. Step 6 is deliberately a
separate change so the fallback removal is not entangled with the rollout.

## Consequences

- **Every task event in all five projects now reaches this service** and is
  written to the DB, the semantic index, and the due-day digest. This is a
  significant widening of what the service ingests, accepted deliberately
  (D2). Mediation and Cheryl content will be embedded into `task_index` and
  summarized by Haiku for digest bullets.
- Webhook volume rises roughly with total task activity rather than default-
  project activity. The existing `_MAX_REFRESH_PER_DELIVERY` cap of 20 already
  bounds per-delivery index work; it may need revisiting if deliveries batch
  more heavily.
- `docs/asana-webhook-setup.md` becomes a fallback runbook rather than the
  primary path; the reconciler is the primary path. It should be updated to
  say so.
- The `repeat:` tag stops being a trap. The inert-tag note written into the
  Pacifica renewal task on 2026-09-08 should be removed once this ships.

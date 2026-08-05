# Task Vector Search — Design

**Date:** 2026-08-05
**Status:** Approved design, pre-implementation

## 1. Problem

Task search today lists tasks live from Asana and substring-filters them in
Python (`services/task_search.py`). Natural-language queries fail unless they
share exact vocabulary with a task's title or notes — "invoices I need to pay"
will not find "Settle the Comcast bill". Asana's free tier has no search API,
so the fix must be built here.

Goal: `POST /search` accepts a natural-language query and returns
nearest-neighbor semantic matches, ranked by similarity.

## 2. Approach (decided)

Own the corpus and vectors; rent only the text→floats computation.

- **Corpus + vectors**: new `task_index` table in the existing `tasks`
  database on the shared Cloud SQL instance (pgvector is already installed
  instance-wide; inbox uses it).
- **Embeddings**: Vertex AI `gemini-embedding-001` via REST, 768 dims.
  Chosen over Voyage/OpenAI (equal cost ~$0.05/mo, but they add a vendor +
  secret; Vertex is IAM-auth with no new secrets) and over local bge-small
  (2GB PyTorch image, cold starts, weakest quality). Decision record in
  the brainstorming session; all hosted options are result-indistinguishable
  at this corpus size (~thousands of tasks).
- **Freshness**: write-through on every create/update path plus the existing
  Asana webhook for edits made directly in Asana. No scheduled sync job —
  a re-runnable backfill script heals drift on demand (YAGNI until proven).

## 3. Schema

Appended to `repo/schema.sql`, applied via the existing `scripts/migrate_db.py`
flow:

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS task_index (
    task_gid      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    project       TEXT,                -- project name, denormalized for filtering
    completed     BOOLEAN NOT NULL DEFAULT false,
    due_on        DATE,
    permalink_url TEXT,
    content_hash  TEXT NOT NULL,       -- sha256(title + notes); skip re-embed when unchanged
    embedding     vector(768),         -- NULL = embed pending/failed; healed by backfill
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- Separate from the existing `tasks` table: that table is email-linkage
  metadata (email-derived tasks only); `task_index` covers **all** tasks,
  including ones created manually in Asana.
- `embedding` nullable: content lands even when Vertex is down; a NULL row is
  invisible to semantic ranking until healed, but still reachable via the
  existing substring path (which reads Asana live).
- `content_hash` makes writes idempotent and backfill cheap — unchanged
  content never re-embeds.
- No HNSW index initially. Exact scan over thousands of rows is
  sub-millisecond; adding the index later is one migration line.

## 4. Embedding client — `clients/vertex.py`

I/O only, per layer rules. One public function:

```python
def embed(text: str, *, task_type: str) -> list[float]:
    # task_type: "RETRIEVAL_DOCUMENT" (indexing) | "RETRIEVAL_QUERY" (searching)
```

- Endpoint: `POST https://{region}-aiplatform.googleapis.com/v1/projects/
  {project}/locations/{region}/publishers/google/models/
  {model}:predict` via `httpx`.
- Auth: bearer token from `google.auth.default()` — metadata server in
  GCP, gcloud ADC locally. Credentials cached module-level, auto-refreshed.
  `google-auth` is already a transitive dependency (Cloud SQL connector);
  pin it explicitly in `requirements.txt`.
- `outputDimensionality: 768`. Truncated Matryoshka vectors are not
  unit-length — the client **renormalizes** before returning (required for
  cosine distance to behave).
- Input truncated to 8,000 chars before sending (model limit 2,048 tokens).
- Config via env vars with stack-matching defaults, not secrets:
  `VERTEX_PROJECT` (default `bens-project-462804`), `VERTEX_REGION`
  (default `us-central1`), `VERTEX_EMBED_MODEL` (default
  `gemini-embedding-001`). Model name in config because switching models
  invalidates all stored vectors (re-run backfill).
- Observability: `vertex.api.duration` histogram + error counter, mirroring
  `clients/asana.py::_request`.

## 5. Index write path

One service function owns the logic; all writers call it:

- `services/task_index.py::upsert(conn, task, embedder)` — computes
  `content_hash`, skips the embed call when the hash matches the stored row,
  otherwise embeds (`RETRIEVAL_DOCUMENT`) and upserts. Pure logic +
  injected I/O, unit-testable with a fake embedder.
- `repo/task_index.py` — DB read/write only, takes an open connection
  (existing repo-layer contract).

Writers:

1. **Pipeline** (`handlers/task_create.py`): after Asana create + existing
   `tasks` insert, upsert the index row. Best-effort: failure logs +
   increments a metric, never crashes the event (a crash would trigger
   Pub/Sub redelivery and duplicate the Asana task).
2. **API** (`api/routers/tasks.py`): create and patch call the same upsert
   after the Asana write succeeds.
3. **Asana webhook** (`handlers/asana_webhook.py::receive`): extend dispatch —
   - `added` events → fetch task from Asana, upsert (catches tasks created
     directly in Asana).
   - `changed` events with field `name`, `notes`, or `due_on` → fetch,
     upsert (catches direct edits).
   - `changed`/`completed` events → additionally flip `task_index.completed`
     alongside the existing `tasks.completed_at` update.
   Per-event cost is one Asana GET + at most one embed call — fine for the
   webhook CF.
4. **Backfill** (`scripts/backfill_embeddings.py`): pages through all
   workspace tasks (reusing the search router's listing helpers), upserts
   everything; embeds only where the hash changed or embedding is NULL.
   Idempotent. Seeds the corpus at rollout and doubles as the drift-healer.

## 6. Search API

`POST /search` (`api/routers/search.py`) gains one request field:

```
semantic: bool = false
```

Explicit, not automatic: the caller (usually the `searching-tasks` skill)
knows whether the query is keywords or natural language. Existing callers
see zero behavior change.

Semantic path:

1. Embed the query (`RETRIEVAL_QUERY`).
2. SQL pre-filter on the denormalized columns (`completed`,
   `due_before`/`due_after`, project when given), then
   `ORDER BY embedding <=> $qvec LIMIT {limit}` (the request's existing
   `limit` field, default 25).
3. Hydrate each hit live from Asana in parallel (existing
   ThreadPoolExecutor pattern) — results stay authoritative; a task deleted
   in Asana since indexing is dropped silently.
4. Existing email-context decoration applies unchanged.

Response: existing `SearchResult` shape plus `score: float` (cosine
similarity, 0–1), and a top-level `semantic: bool` echoing which path served
the request.

**Fallback rule:** if the embed call fails at query time, fall back to the
existing substring path and set `semantic: false` in the response so the
caller knows ranking degraded. A search request never 5xxs because Vertex
is down.

## 7. Infra

- `terraform/main.tf`: add `aiplatform.googleapis.com` to the enabled
  services.
- `terraform/iam.tf`: grant `roles/aiplatform.user` to the CF service
  account and the Cloud Run (tasks-api) service account.
- No new secrets, no tfvars changes, no new scheduled jobs.

## 8. Testing

- Unit tests with a fake embedder (deterministic vectors): upsert
  idempotency, hash-skip behavior, ranking order, filter + ranking
  interaction, query-time fallback, webhook dispatch for the new event
  types. No network in tests (existing convention).
- Local smoke: `scripts/test-api-local.py` gains a semantic-search check;
  backfill script runnable against the real DB with `--dry-run`.

## 9. Rollout

1. Migrate DB (`CREATE EXTENSION vector` + table).
2. Terraform apply (API enablement + IAM).
3. Deploy CF + tasks-api.
4. Run `scripts/backfill_embeddings.py` to seed the corpus.
5. Update the `searching-tasks` skill to pass `semantic: true` for
   natural-language queries.

Each step is independently safe — before backfill, semantic search simply
returns few/no results.

## 10. Deferred

- HNSW index — add when corpus size makes exact scan measurable.
- Scheduled reconciliation sync — add if webhook-driven freshness proves
  leaky; the backfill script is the manual stopgap.
- Hybrid ranking (FTS + vector fusion) — revisit if pure semantic ranking
  disappoints on exact-keyword queries.
- Porting the pattern to ~/src/docs (its spec §15 already defers exactly
  this; `clients/vertex.py` is written to be lift-and-shift, with chunking
  as the docs-specific addition).

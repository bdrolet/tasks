CREATE TABLE IF NOT EXISTS tasks (
    task_gid     TEXT PRIMARY KEY,
    message_id   TEXT UNIQUE NOT NULL,
    category     TEXT NOT NULL,
    importance   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    escalated_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS asana_tag_cache (
    tag_name TEXT PRIMARY KEY,
    tag_gid  TEXT NOT NULL
);

-- Semantic-search corpus: one row per workspace task (incl. manual ones).
-- embedding NULL = embed pending/failed; healed by scripts/backfill_embeddings.py.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS task_index (
    task_gid      TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    project       TEXT,
    completed     BOOLEAN NOT NULL DEFAULT false,
    due_on        DATE,
    permalink_url TEXT,
    content_hash  TEXT NOT NULL,
    embedding     vector(768),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Gate-2 audit trail: emails that passed the policy gate but were judged
-- moot by the triage agent (source='agent') or the no-action phrase veto
-- (source='phrase'). related_task_gid is set when the email was attached
-- to an existing task as a comment instead. evidence = the agent's list.
CREATE TABLE IF NOT EXISTS suppressed_emails (
    message_id       TEXT PRIMARY KEY,
    category         TEXT NOT NULL,
    importance       TEXT NOT NULL,
    subject          TEXT,
    sender           TEXT,
    reason           TEXT NOT NULL,
    source           TEXT NOT NULL,
    related_task_gid TEXT,
    evidence         JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Due-day digest (docs/superpowers/specs/2026-09-03-due-day-digest-design.md).
-- One row per calendar event this service created for a (day, calendar):
-- REQUIRED for the digest to run — without it we cannot tell our events
-- from anything else on the calendar, so a DB outage skips the rebuild.
CREATE TABLE IF NOT EXISTS due_day_events (
    day          DATE  NOT NULL,
    calendar_id  TEXT  NOT NULL,
    event_id     TEXT  NOT NULL,
    content_hash TEXT  NOT NULL,
    task_gids    JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, calendar_id)
);

-- Haiku-condensed bullets per task, keyed on a hash of name + html_notes.
CREATE TABLE IF NOT EXISTS task_bullets (
    task_gid     TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    bullets      JSONB NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Single-row rebuild state: the Asana webhook sets dirty_at; POST /digest
-- rebuilds when dirty_at > last_rebuilt_at or the last rebuild is stale.
CREATE TABLE IF NOT EXISTS digest_state (
    id              BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    dirty_at        TIMESTAMPTZ,
    last_rebuilt_at TIMESTAMPTZ
);

-- One Asana webhook per managed project, each with its own X-Hook-Secret
-- (docs/superpowers/specs/2026-09-08-cross-project-recurrence-design.md, D3).
-- Written at handshake time keyed on project_gid, because the webhook gid
-- does not exist until the registering POST returns.
CREATE TABLE IF NOT EXISTS asana_webhooks (
    project_gid   TEXT PRIMARY KEY,
    webhook_gid   TEXT,
    secret        TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deterministic Asana facts for every task in a managed project (and their
-- subtasks). Rewritten on every gather. priority is the [PX] prefix or NULL.
CREATE TABLE IF NOT EXISTS task_facts (
    task_gid           TEXT PRIMARY KEY,
    project_gid        TEXT,
    project_name       TEXT,
    parent_gid         TEXT,
    name               TEXT NOT NULL,
    permalink_url      TEXT,
    priority           TEXT,                 -- 'P0'..'P3' or NULL
    due_on             DATE,
    due_at             TIMESTAMPTZ,
    start_on           DATE,
    started_at         DATE,                 -- "Started at" custom field
    story_points       INTEGER,              -- "Story points" custom field
    points_estimated   INTEGER,              -- what enrichment wrote, once; NULL = never
    completed          BOOLEAN NOT NULL DEFAULT false,
    completed_at       TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL,
    modified_at        TIMESTAMPTZ NOT NULL,
    tags               JSONB NOT NULL DEFAULT '[]',   -- names
    dependencies       JSONB NOT NULL DEFAULT '[]',   -- gids
    dependents         JSONB NOT NULL DEFAULT '[]',   -- gids
    num_open_subtasks  INTEGER NOT NULL DEFAULT 0,
    content_hash       TEXT NOT NULL,        -- D5 hash of the text this gather saw
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Model output, cached by content hash. Never carries manual state.
CREATE TABLE IF NOT EXISTS task_enrichment (
    task_gid     TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    raw          JSONB NOT NULL,             -- verbatim model JSON
    model        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Manual state, written only by PUT /tasks/{gid}/overrides (D5 field
-- overrides; D11 pinned_rank / snooze_until). Survives re-enrichment and
-- re-gathering; pinned_rank is cleared by the subscriber on completion.
CREATE TABLE IF NOT EXISTS task_overrides (
    task_gid     TEXT PRIMARY KEY,
    overrides    JSONB NOT NULL DEFAULT '{}',   -- {waiting_on, impact, energy, story_points, due_date_inferred, ...}
    pinned_rank  INTEGER,
    snooze_until DATE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The whole scored set, rewritten on every rescore. GET /ranking and
-- POST /next read only this.
CREATE TABLE IF NOT EXISTS task_scores (
    task_gid     TEXT PRIMARY KEY,
    scored_at    TIMESTAMPTZ NOT NULL,
    today        DATE NOT NULL,
    bucket       TEXT NOT NULL,   -- 'next' | 'nudge' | 'snoozed' | 'excluded:blocked' | 'excluded:parent' | 'excluded:completed'
    score        DOUBLE PRECISION,
    position     INTEGER NOT NULL, -- 1-based order in the full ranking (pins first at their rank, then by score)
    rank         INTEGER,         -- position within the default `next` selection, NULL if not selected
    components   JSONB NOT NULL,  -- P,U,I,B,A,C, points, points_source, effort_days, effective_due, soft,
                                  -- days_until_due, slack, simulated_start, effective_slack, days_stale,
                                  -- energy, unenriched, override {pinned_rank?, snooze_until?, fields: [...]}
    overcommitted BOOLEAN NOT NULL DEFAULT false,
    stale         BOOLEAN NOT NULL DEFAULT false,
    stale_reason  TEXT
);

-- Run log. Event runs keep the top-N; the daily run keeps the full set and
-- is the one deferrals are counted against (D8).
CREATE TABLE IF NOT EXISTS prioritize_runs (
    run_id      BIGSERIAL PRIMARY KEY,
    kind        TEXT NOT NULL,               -- 'event' | 'daily' | 'manual'
    ran_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    today       DATE NOT NULL,
    trigger_gid TEXT,
    top         JSONB NOT NULL               -- [{gid, rank, score, components, started}]
);

-- Feedback: deferrals and the completion snapshot calibrate reads.
CREATE TABLE IF NOT EXISTS task_stats (
    task_gid             TEXT PRIMARY KEY,
    project_name         TEXT,
    times_deferred       INTEGER NOT NULL DEFAULT 0,
    last_offered         DATE,
    started_at           DATE,
    completed_at         TIMESTAMPTZ,
    points_at_completion INTEGER,
    points_estimated     INTEGER,
    cycle_days           DOUBLE PRECISION     -- completed_at - started_at, NULL if never started
);

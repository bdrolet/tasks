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

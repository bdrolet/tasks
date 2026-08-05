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

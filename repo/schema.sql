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

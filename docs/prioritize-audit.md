# Auditing the prioritizer

Where to look after the prioritizer has run for a while, to check what it
offered, why, and what was done about it.

## What is recorded

| Table | What | Written by |
|---|---|---|
| `prioritize_runs` | One row per scoring run: `kind` (`daily` \| `event` \| `manual`), `today`, `trigger_gid`, `top` (the picks, with score and components), `config_hash` | every rescore; `POST /next` |
| `task_scores_history` | The **full** ranking, every task and bucket, as each `daily` run left it, keyed `(run_id, task_gid)` | the daily run (`day_changed`, 05:45 ET) |
| `task_override_events` | Every pin, snooze, points or field override: `patch` (what was asked for, `null` = cleared), `result` (the row afterwards), `source` (`api` \| `completion`) | `PUT /tasks/{gid}/overrides`; the automatic unpin when a task completes |
| `task_stats` | Running totals: `times_deferred`, `last_offered`, and a snapshot taken at completion | the daily run's deferral step and completion events |

`config_hash` is the first 12 hex digits of the sha256 of
`config/prioritize.toml`. When it changes between two runs, the weights
changed, so a jump in scores there may be a retune rather than a change in the
tasks. Runs from before the column existed have `NULL`.

Event runs keep only their top 10 picks and are not snapshotted. The daily
snapshot is the per-day record. `task_scores` itself holds only the latest
rescore.

## Queries

Connect via the `psql-database` skill or Cloud SQL Studio (`tasks` DB).

**What was offered each day, and what happened to it**

```sql
SELECT r.today, e->>'rank' AS rank, f.name, (e->>'started')::bool AS started
FROM prioritize_runs r
CROSS JOIN LATERAL jsonb_array_elements(r.top) e
LEFT JOIN task_facts f ON f.task_gid = e->>'gid'
WHERE r.kind = 'daily'
ORDER BY r.today DESC, (e->>'rank')::int;
```

`started` is filled in the next morning. `null` means that day has not been
settled yet.

**One task's position over time**

```sql
SELECT h.today, h.bucket, h.position, h.rank, round(h.score::numeric, 3) AS score,
       h.components->>'effective_due' AS effective_due, h.stale_reason
FROM task_scores_history h
WHERE h.task_gid = '<gid>'
ORDER BY h.today;
```

**What just missed the cut on a given day**

```sql
SELECT h.position, f.name, round(h.score::numeric, 3) AS score
FROM task_scores_history h
JOIN prioritize_runs r USING (run_id)
LEFT JOIN task_facts f USING (task_gid)
WHERE r.kind = 'daily' AND r.today = '<yyyy-mm-dd>'
  AND h.bucket = 'next' AND h.rank IS NULL
ORDER BY h.position
LIMIT 10;
```

**Manual overrides, newest first**

```sql
SELECT o.at, f.name, o.source, o.patch, o.result
FROM task_override_events o
LEFT JOIN task_facts f USING (task_gid)
ORDER BY o.at DESC;
```

**Retunes: where the config changed**

```sql
SELECT * FROM (
  SELECT ran_at, kind, config_hash,
         lag(config_hash) OVER (ORDER BY ran_at) AS previous
  FROM prioritize_runs WHERE config_hash IS NOT NULL
) x WHERE config_hash IS DISTINCT FROM previous
ORDER BY ran_at;
```

**What `POST /next` showed you**

```sql
SELECT r.ran_at, e->>'rank' AS rank, f.name
FROM prioritize_runs r
CROSS JOIN LATERAL jsonb_array_elements(r.top) e
LEFT JOIN task_facts f ON f.task_gid = e->>'gid'
WHERE r.kind = 'manual'
ORDER BY r.ran_at DESC, (e->>'rank')::int;
```

Manual runs are a record only. Deferrals and cross-day fairness count the
daily pick, not what `/next` returned.

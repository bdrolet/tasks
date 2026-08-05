"""task_index read/write — the semantic-search corpus. Takes an open
connection. Vectors cross the wire as '[x,y,...]' text with an explicit
::vector cast so both drivers (pg8000 in prod, psycopg locally) behave."""

from typing import Any


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(str(x) for x in embedding) + "]"


def get_state(conn: Any, task_gid: str) -> dict | None:
    return conn.execute(
        "SELECT content_hash, (embedding IS NOT NULL) AS has_embedding"
        " FROM task_index WHERE task_gid = %s",
        (task_gid,),
    ).fetchone()


def upsert(
    conn: Any,
    *,
    task_gid: str,
    title: str,
    notes: str,
    project: str | None,
    completed: bool,
    due_on: str | None,
    permalink_url: str | None,
    content_hash: str,
    embedding: list[float] | None,
) -> None:
    """Full-row upsert. embedding=None preserves any existing vector (the
    caller failed to embed; a stale vector beats no vector)."""
    conn.execute(
        """
        INSERT INTO task_index
            (task_gid, title, notes, project, completed, due_on, permalink_url,
             content_hash, embedding, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, now())
        ON CONFLICT (task_gid) DO UPDATE SET
            title         = EXCLUDED.title,
            notes         = EXCLUDED.notes,
            project       = EXCLUDED.project,
            completed     = EXCLUDED.completed,
            due_on        = EXCLUDED.due_on,
            permalink_url = EXCLUDED.permalink_url,
            content_hash  = EXCLUDED.content_hash,
            embedding     = COALESCE(EXCLUDED.embedding, task_index.embedding),
            updated_at    = now()
        """,
        (
            task_gid,
            title,
            notes,
            project,
            completed,
            due_on,
            permalink_url,
            content_hash,
            _vec(embedding) if embedding is not None else None,
        ),
    )


def set_completed(conn: Any, task_gid: str, completed: bool) -> None:
    conn.execute(
        "UPDATE task_index SET completed = %s, updated_at = now() WHERE task_gid = %s",
        (completed, task_gid),
    )


def delete(conn: Any, task_gid: str) -> None:
    conn.execute("DELETE FROM task_index WHERE task_gid = %s", (task_gid,))


def semantic_candidates(
    conn: Any,
    *,
    query_embedding: list[float],
    completed: bool | None,
    due_before: str | None,
    due_after: str | None,
    project: str | None,
    limit: int,
) -> list[dict]:
    """Nearest neighbors by cosine distance, best score first. Date-bound
    semantics match the substring path (inclusive; undated dropped). Note:
    subtasks are indexed with project=NULL (no memberships), so a project
    filter excludes them — unlike the substring path, which sweeps a
    project's subtasks."""
    where = ["embedding IS NOT NULL"]
    params: list = [_vec(query_embedding)]
    if completed is not None:
        where.append("completed = %s")
        params.append(completed)
    if project is not None:
        where.append("project = %s")
        params.append(project)
    if due_before is not None:
        where.append("due_on IS NOT NULL AND due_on <= %s")
        params.append(due_before)
    if due_after is not None:
        where.append("due_on IS NOT NULL AND due_on >= %s")
        params.append(due_after)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT task_gid, 1 - (embedding <=> %s::vector) AS score
        FROM task_index
        WHERE {" AND ".join(where)}
        ORDER BY score DESC
        LIMIT %s
        """,
        tuple(params),
    ).fetchall()

from typing import Any


def get_gid(conn: Any, tag_name: str) -> str | None:
    """Return the cached Asana GID for tag_name, or None if not cached."""
    row = conn.execute(
        "SELECT tag_gid FROM asana_tag_cache WHERE tag_name = %s",
        (tag_name,),
    ).fetchone()
    return row["tag_gid"] if row else None


def store_gid(conn: Any, tag_name: str, tag_gid: str) -> None:
    """Upsert a tag_name → tag_gid mapping into the local cache."""
    conn.execute(
        """
        INSERT INTO asana_tag_cache (tag_name, tag_gid)
        VALUES (%s, %s)
        ON CONFLICT (tag_name) DO NOTHING
        """,
        (tag_name, tag_gid),
    )

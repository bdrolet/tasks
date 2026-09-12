"""Per-project Asana webhook secrets.

Asana mints one X-Hook-Secret per webhook and it cannot be supplied by the
caller, so N managed projects means N secrets. They live here rather than in
Secret Manager because the webhook CF is publicly invokable by necessity and
must not hold secretmanager.versions.add (D3).

The row is written at handshake time, when the webhook gid does not exist yet
— the project gid from the target's query string is the only key available
(D4). The reconciler fills webhook_gid in afterwards.
"""

from typing import Any


def upsert_secret(conn: Any, project_gid: str, secret: str) -> None:
    """Store the handshake secret for a project, replacing any previous one.

    webhook_gid is cleared: a new handshake means a new webhook, and the
    reconciler has not yet learned its gid."""
    conn.execute(
        """
        INSERT INTO asana_webhooks (project_gid, secret, registered_at)
        VALUES (%s, %s, now())
        ON CONFLICT (project_gid) DO UPDATE
            SET secret = EXCLUDED.secret,
                webhook_gid = NULL,
                registered_at = now()
        """,
        (project_gid, secret),
    )


def set_webhook_gid(conn: Any, project_gid: str, webhook_gid: str) -> None:
    conn.execute(
        "UPDATE asana_webhooks SET webhook_gid = %s WHERE project_gid = %s",
        (webhook_gid, project_gid),
    )


def get_secret(conn: Any, project_gid: str) -> str | None:
    row = conn.execute(
        "SELECT secret FROM asana_webhooks WHERE project_gid = %s",
        (project_gid,),
    ).fetchone()
    return row["secret"] if row else None


def list_all(conn: Any) -> list[dict]:
    return conn.execute(
        "SELECT project_gid, webhook_gid, secret FROM asana_webhooks ORDER BY project_gid"
    ).fetchall()


def delete(conn: Any, project_gid: str) -> None:
    conn.execute("DELETE FROM asana_webhooks WHERE project_gid = %s", (project_gid,))

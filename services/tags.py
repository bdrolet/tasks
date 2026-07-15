"""Resolve tag names to Asana GIDs using the DB cache, falling back to
Asana typeahead lookup / tag creation (migrated from inbox asana_tag_cache)."""

import clients.asana as asana
from clients.db import get_conn
from repo import asana_tags


def resolve_gids(tag_names: list[str]) -> list[str]:
    if not tag_names or not asana.ASANA_API_KEY:
        return []
    gids = []
    with get_conn() as conn:
        for name in tag_names:
            gid = asana_tags.get_gid(conn, name)
            if not gid:
                workspace_gid = asana.get_workspace_gid()
                gid = asana.find_tag(name, workspace_gid) or asana.create_tag(name, workspace_gid)
                asana_tags.store_gid(conn, name, gid)
            gids.append(gid)
    return gids

"""Strategy read side and its three small writes: the weekly review, lag
reports, area mutes, and restoring a necessity suppression (spec D11, D12,
D14). Reads goal_state/task_scores only; the restore is the one Asana write."""

from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, ConfigDict

import clients.asana as asana
import clients.pubsub as pubsub
from clients.db import get_conn
from models.strategy import Strategy
from repo import goals as repo_goals
from repo import prioritize as repo
from repo import suppressions as repo_sup
from services import review, task_index
from services import tags as tags_service
from services.due_digest import today_local

router = APIRouter()


def _today() -> date:
    return today_local()


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    period_start: date | None = None


class MuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: date | None


def _strategy() -> Strategy:
    """The daily tick's snapshot (spec D1): this service reads rows, never
    the mounted secret."""
    with get_conn() as conn:
        return repo_goals.load_snapshot(conn)


@router.get("/review")
def get_review() -> dict:
    strategy = _strategy()
    today = _today()
    with get_conn() as conn:
        states = repo_goals.get_states(conn, today)
        if not states:  # before today's tick: show yesterday's
            states = repo_goals.get_states(conn, today - timedelta(days=1))
        scores = repo.list_scores(conn)
        suppressed = repo_sup.list_necessity(conn)
    return review.build(strategy, states, scores, suppressed, today)


@router.post("/goals/{goal_id}/reports", status_code=201)
def post_report(goal_id: str, body: ReportRequest) -> dict:
    goal = _strategy().get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail=f"unknown goal: {goal_id}")
    if goal.kind != "outcome":
        raise HTTPException(status_code=400, detail="only an outcome goal has a lag measure")
    with get_conn() as conn:
        rid = repo_goals.insert_report(conn, goal_id, float(body.value), body.period_start)
    return {"id": rid, "goal_id": goal_id, "value": float(body.value),
            "period_start": body.period_start.isoformat() if body.period_start else None}


@router.post("/goals/{goal_id}/mute")
def post_mute(goal_id: str, body: MuteRequest) -> dict:
    goal = _strategy().get(goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail=f"unknown goal: {goal_id}")
    if goal.kind != "area":
        raise HTTPException(status_code=400, detail="only an area can be muted")
    with get_conn() as conn:
        repo_goals.set_mute(conn, goal_id, body.until)
    return {"goal_id": goal_id, "mute_until": body.until.isoformat() if body.until else None}


@router.post("/suppressions/{message_id}/restore", status_code=201)
def restore(message_id: str, response: Response) -> dict:
    with get_conn() as conn:
        row = repo_sup.get(conn, message_id)
    if row is None or row.get("source") != "necessity":
        raise HTTPException(status_code=404, detail="no necessity suppression for that message")
    if row.get("restored_at"):
        response.status_code = 200
        return {"task_gid": row["restored_task_gid"], "permalink_url": None, "message_id": message_id,
                "already_restored": True}
    existing = asana.find_task_by_external(message_id)
    if existing:
        gid, url = existing, None
    else:
        subject = row.get("subject") or "(no subject)"
        fields = {
            "name": f"[{row.get('importance') or 'P2'}] {subject}",
            "html_notes": (
                f"<body>Restored from a necessity suppression. From {row.get('sender') or '?'}.\n"
                f"Suppressed because: {row.get('reason') or '—'}\n"
                f"<a href=\"{row.get('web_link') or ''}\">Open the email</a></body>"
            ),
            "projects": [asana.ASANA_PROJECT_ID],
            "external": {"gid": message_id, "data": "inbox"},
        }
        tag_gids = tags_service.resolve_gids(["restored"])
        if tag_gids:
            fields["tags"] = tag_gids
        created = asana.create_task_from_fields(fields)
        gid, url = created.gid, created.permalink_url
    with get_conn() as conn:
        repo_sup.mark_restored(conn, message_id, gid)
    task_index.refresh(gid)
    pubsub.publish_task_changed(gid, "api")
    return {"task_gid": gid, "permalink_url": url, "message_id": message_id}

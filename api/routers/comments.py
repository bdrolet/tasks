from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import clients.asana as asana
from api.auth import verify_token
from api.errors import translate_asana_errors
from api.routers.tasks import wrap_html_body

router = APIRouter()


class CommentBody(BaseModel):
    text: str | None = None
    html_text: str | None = None


def _validated(body: CommentBody) -> CommentBody:
    if (body.text is None) == (body.html_text is None):
        raise HTTPException(status_code=400, detail="pass exactly one of text or html_text")
    if body.html_text is not None:
        body.html_text = wrap_html_body(body.html_text)
    return body


@router.post("/tasks/{gid}/comments", status_code=201)
def add_comment(gid: str, body: CommentBody, _: None = Depends(verify_token)) -> dict:
    body = _validated(body)
    with translate_asana_errors():
        story = asana.create_story(gid, text=body.text, html_text=body.html_text)
    return {"comment_gid": story["gid"], "text": story.get("text")}


@router.put("/comments/{story_gid}")
def edit_comment(story_gid: str, body: CommentBody, _: None = Depends(verify_token)) -> dict:
    body = _validated(body)
    with translate_asana_errors():
        asana.update_story(story_gid, text=body.text, html_text=body.html_text)
    return {"status": "updated"}


@router.delete("/comments/{story_gid}")
def delete_comment(story_gid: str, _: None = Depends(verify_token)) -> dict:
    with translate_asana_errors():
        asana.delete_story(story_gid)
    return {"status": "deleted"}

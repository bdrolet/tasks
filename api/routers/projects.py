from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

import clients.asana as asana
from api.auth import verify_token
from api.errors import translate_asana_errors

router = APIRouter()


class SectionInfo(BaseModel):
    gid: str
    name: str


class ProjectInfo(BaseModel):
    gid: str
    name: str
    sections: list[SectionInfo] = []


class ProjectsResponse(BaseModel):
    projects: list[ProjectInfo]


class TagInfo(BaseModel):
    gid: str
    name: str


class TagsResponse(BaseModel):
    tags: list[TagInfo]


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1)
    sections: list[str] = []


class CreatedProjectResponse(BaseModel):
    project_gid: str
    permalink_url: str | None = None
    sections: dict[str, str] = {}


@router.get("/projects", response_model=ProjectsResponse)
def get_projects(_: None = Depends(verify_token)) -> ProjectsResponse:
    with translate_asana_errors():
        projects = asana.list_projects()
        out = [
            ProjectInfo(
                gid=p["gid"],
                name=p["name"],
                sections=[SectionInfo(gid=s["gid"], name=s["name"])
                          for s in asana.get_sections(p["gid"])],
            )
            for p in projects
        ]
    return ProjectsResponse(projects=out)


@router.get("/tags", response_model=TagsResponse)
def get_tags(_: None = Depends(verify_token)) -> TagsResponse:
    with translate_asana_errors():
        tags = asana.list_tags()
    return TagsResponse(tags=[TagInfo(gid=t["gid"], name=t["name"]) for t in tags])


@router.post("/projects", response_model=CreatedProjectResponse, status_code=201)
def create_project(
    body: CreateProjectRequest, _: None = Depends(verify_token)
) -> CreatedProjectResponse:
    with translate_asana_errors():
        result = asana.create_project(body.name, body.sections)
    return CreatedProjectResponse(
        project_gid=result["gid"],
        permalink_url=result.get("permalink_url"),
        sections=result["sections"],
    )

"""笔记版本历史。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ... import responses as r
from ... import service
from .deps import CurrentUser, get_manager

router = APIRouter()


@router.get("/jobs/{job_id}/versions", tags=["笔记"], summary="笔记版本历史", response_model=r.VersionsResponse)
def note_versions(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"versions": service.note_versions(get_manager(request), job_id, user)}


@router.get(
    "/jobs/{job_id}/versions/{version}",
    tags=["笔记"],
    summary="某个版本的 Markdown 原文",
    response_model=r.NoteVersionText,
)
def note_version(
    job_id: str, version: int, request: Request, user: CurrentUser, kind: str = "detailed"
) -> dict[str, Any]:
    return service.note_version_text(get_manager(request), job_id, version, kind, user)


@router.post(
    "/jobs/{job_id}/versions/{version}/restore",
    tags=["笔记"],
    summary="恢复到某个版本（另存为新版本）",
    response_model=r.JobResponse,
)
def restore_version(job_id: str, version: int, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.restore_note_version(get_manager(request), job_id, version, user)

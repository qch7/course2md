"""任务产物：文件下载、预览与 Anki 导出。"""

from __future__ import annotations

import mimetypes
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ... import responses as r
from ... import service
from .deps import CurrentUser, get_manager

router = APIRouter()


@router.get("/jobs/{job_id}/export/anki", tags=["导出"], response_class=Response)
def export_anki(job_id: str, request: Request, user: CurrentUser) -> Response:
    from ....export import anki_text

    job = service.get_job(get_manager(request), job_id, user)
    with job.locked():
        if not job.result or not job.result.note_workspace:
            raise HTTPException(400, detail="课程尚未生成笔记")
        text = anki_text(job.result.note_workspace)
    return Response(
        text,
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(job.title, safe='')}.anki.tsv"},
    )


@router.get("/jobs/{job_id}/file", tags=["产物"], summary="下载任务目录中的文件", response_class=FileResponse)
def job_file(job_id: str, request: Request, user: CurrentUser, rel: str = "", inline: str = "0") -> FileResponse:
    target = service.job_file(service.get_job(get_manager(request), job_id, user), rel)
    show_inline = inline == "1" and target.suffix.lower() in service.IMAGE_SUFFIXES
    return FileResponse(
        target,
        media_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream",
        headers={
            "Content-Disposition": f"{'inline' if show_inline else 'attachment'}; "
            f"filename*=UTF-8''{quote(target.name)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/jobs/{job_id}/preview", tags=["产物"], summary="预览文本、Markdown 或图片", response_model=r.Preview)
def job_preview(job_id: str, request: Request, user: CurrentUser, rel: str = "") -> dict[str, Any]:
    return service.preview(service.get_job(get_manager(request), job_id, user), rel)

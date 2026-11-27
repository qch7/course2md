"""课程产物打包与 Hub 间互传。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ... import responses as r
from ... import service
from ...schemas import (
    RemoteCatalogRequest,
    TransferPeerCreateRequest,
    TransferPeerUpdateRequest,
    TransferPullRequest,
    TransferPushRequest,
)
from .deps import CurrentUser, get_manager

router = APIRouter()


@router.get(
    "/jobs/{job_id}/bundle",
    tags=["互传"],
    summary="导出单个课次的便携产物包（.c2m.zip）",
    response_class=FileResponse,
)
def export_job_bundle(
    job_id: str,
    request: Request,
    user: CurrentUser,
    include_cache: bool = Query(False, description="同时带上各阶段缓存，导入后可从中间阶段重新运行"),
) -> FileResponse:
    path, filename = service.export_job_bundle_file(get_manager(request), job_id, user, include_cache=include_cache)
    return FileResponse(
        path,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.get(
    "/courses/{course_id}/bundle",
    tags=["互传"],
    summary="导出整门课程或指定课次的便携产物包（.c2m.zip）",
    response_class=FileResponse,
)
def export_course_bundle(
    course_id: str,
    request: Request,
    user: CurrentUser,
    job_ids: str = Query("", description="逗号分隔的课次任务编号；留空导出全部已完成课次"),
    include_cache: bool = Query(False, description="同时带上各阶段缓存，导入后可从中间阶段重新运行"),
) -> FileResponse:
    selected = [item.strip() for item in job_ids.split(",") if item.strip()] if job_ids else None
    path, filename = service.export_course_bundle_file(
        get_manager(request), course_id, selected, user, include_cache=include_cache
    )
    return FileResponse(
        path,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.post(
    "/bundles/import",
    tags=["互传"],
    summary="导入课程产物包（.c2m.zip，流式上传，最大 512 MB）",
    response_model=r.BundleImportResponse,
)
async def import_bundle(
    request: Request,
    user: CurrentUser,
    conflict: str = Query("skip", description="冲突处理：skip（跳过）、overwrite（覆盖）、copy（另存副本）"),
    out_dir: str | None = Query(None, description="本地模式可选输出根目录"),
) -> dict[str, Any]:
    from ....jobs.bundle import MAX_BUNDLE_BYTES

    fd, tmp_name = tempfile.mkstemp(prefix="c2m-upload-", suffix=".c2m.zip")
    tmp_path = Path(tmp_name)
    total = 0
    try:
        with os.fdopen(fd, "wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BUNDLE_BYTES:
                    raise ValueError(f"产物包大小超过上限（{MAX_BUNDLE_BYTES // (1024 * 1024)} MB）")
                output.write(chunk)
        if total == 0:
            raise ValueError("上传的产物包为空")
        return await asyncio.to_thread(
            service.import_bundle_file,
            get_manager(request),
            tmp_path,
            user=user,
            conflict=conflict,
            out_dir=out_dir,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/transfer/peers", tags=["互传"], summary="已保存的对端 Hub 列表", response_model=r.TransferPeersResponse)
def list_transfer_peers(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_transfer_peers(get_manager(request), user)


@router.post("/transfer/peers", tags=["互传"], summary="保存一个对端 Hub", response_model=r.TransferPeerResponse)
def create_transfer_peer(body: TransferPeerCreateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_transfer_peer(get_manager(request), body.payload(), user)


@router.patch(
    "/transfer/peers/{peer_id}", tags=["互传"], summary="修改已保存的对端 Hub", response_model=r.TransferPeerResponse
)
def update_transfer_peer(
    peer_id: str, body: TransferPeerUpdateRequest, request: Request, user: CurrentUser
) -> dict[str, Any]:
    return service.update_transfer_peer(get_manager(request), peer_id, body.payload(), user)


@router.delete("/transfer/peers/{peer_id}", tags=["互传"], summary="删除已保存的对端 Hub", response_model=r.Ok)
def delete_transfer_peer(peer_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_transfer_peer(get_manager(request), peer_id, user)


@router.post(
    "/transfer/remote",
    tags=["互传"],
    summary="连接对端 Hub 并列出可拉取的课程与课次",
    response_model=r.RemoteCatalogResponse,
)
def remote_catalog(body: RemoteCatalogRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.remote_catalog(get_manager(request), body.payload(), user)


@router.post(
    "/transfer/push",
    tags=["互传"],
    summary="将本 Hub 的课程或课次产物推送到对端 Hub",
    response_model=r.TransferResultResponse,
)
def transfer_push(body: TransferPushRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.transfer_push(get_manager(request), body.payload(), user)


@router.post(
    "/transfer/pull",
    tags=["互传"],
    summary="从对端 Hub 拉取课程或课次产物并导入本 Hub",
    response_model=r.TransferResultResponse,
)
def transfer_pull(body: TransferPullRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.transfer_pull(get_manager(request), body.payload(), user)

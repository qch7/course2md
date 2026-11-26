"""REST 接口 v1。

全部路由位于 ``/api/v1`` 下。
破坏性改动只进入 v2。
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask

from ...core.accounts import User
from .. import plugins, service
from .. import responses as r
from ..auth import current_user
from ..schemas import (
    CreateJobRequest,
    HubLinkCreateRequest,
    HubLinkUpdateRequest,
    MessageRequest,
    PluginInstallRequest,
    RefreshRequest,
    RemoteCatalogRequest,
    RerunRequest,
    ResolveRequest,
    TransferPeerCreateRequest,
    TransferPeerUpdateRequest,
    TransferPullRequest,
    TransferPushRequest,
    WorkerTokenRequest,
)

router = APIRouter()
CurrentUser = Annotated[User, Depends(current_user)]
TERMINAL = ("done", "failed", "cancelled")


def _manager(request: Request):
    return request.app.state.manager


@router.get("/plugins", tags=["插件"], summary="已安装的来源解析插件与管理状态", response_model=r.PluginsResponse)
def list_plugins(request: Request, user: CurrentUser) -> dict[str, Any]:
    return plugins.list_plugins(_manager(request), user)


@router.post(
    "/plugins", tags=["插件"], summary="从 GitHub 或本机路径安装插件（本地应用）", response_model=r.PluginsResponse
)
def install_plugin(body: PluginInstallRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return plugins.install_plugin(_manager(request), body.source, body.kind, user)


@router.post(
    "/plugins/import", tags=["插件"], summary="上传插件安装包（本地应用，最大 32 MB）", response_model=r.PluginsResponse
)
def import_plugin(
    request: Request,
    user: CurrentUser,
    filename: str = Query(..., description=".whl、.zip 或 .tar.gz 文件名"),
    content: bytes = Body(..., media_type="application/octet-stream"),
) -> dict[str, Any]:
    return plugins.import_plugin(_manager(request), filename, content, user)


@router.delete(
    "/plugins/{name}", tags=["插件"], summary="卸载来源解析插件（本地应用）", response_model=r.PluginsResponse
)
def delete_plugin(name: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return plugins.delete_plugin(_manager(request), name, user)


@router.get("/config", tags=["配置"], summary="本机配置、默认值与可选项", response_model=r.Config)
def get_config(request: Request, user: CurrentUser) -> dict[str, Any]:
    payload = service.config_payload(_manager(request), user)
    if service.server_mode(_manager(request)):
        # 服务器路径与模型位置只对管理员显示
        payload["server"] = True
        if not user.is_admin:
            payload.pop("default_out", None)
            payload.pop("model_path", None)
    return payload


@router.post("/resolve", tags=["课程"], summary="解析来源，列出课次并保存课程", response_model=r.ResolvedCourse)
def resolve(body: ResolveRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.resolve_source(_manager(request), body.payload(), user)


@router.get("/courses", tags=["课程"], summary="已保存的课程", response_model=r.CoursesResponse)
def list_courses(request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"courses": service.list_courses(_manager(request), user)}


@router.get("/courses/{course_id}", tags=["课程"], summary="课程详情与课次状态", response_model=r.CourseResponse)
def get_course(course_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    manager = _manager(request)
    return {"course": manager.course_snapshot(service.get_course(manager, course_id, user))}


@router.delete(
    "/courses/{course_id}", tags=["课程"], summary="删除课程（移入回收站，30 天后永久删除）", response_model=r.Ok
)
def delete_course(course_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_course(_manager(request), course_id, user)


@router.get("/trash", tags=["回收站"], summary="回收站中的课程与课次", response_model=r.TrashResponse)
def list_trash(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_trash(_manager(request), user)


@router.post("/trash/{kind}/{item_id}/restore", tags=["回收站"], summary="从回收站恢复课程或课次", response_model=r.Ok)
def restore_trash(kind: str, item_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.restore_trash(_manager(request), kind, item_id, user)


@router.delete("/trash/{kind}/{item_id}", tags=["回收站"], summary="立即永久删除回收站中的项目", response_model=r.Ok)
def purge_trash(kind: str, item_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.purge_trash(_manager(request), kind, item_id, user)


@router.post(
    "/courses/{course_id}/refresh", tags=["课程"], summary="重新解析已保存课程的课次", response_model=r.ResolvedCourse
)
def refresh_course(course_id: str, body: RefreshRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.refresh_course(_manager(request), course_id, body.payload(), user)


@router.get("/jobs", tags=["任务"], summary="全部任务", response_model=r.JobsResponse)
def list_jobs(request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"jobs": service.list_jobs(_manager(request), user)}


@router.post(
    "/jobs",
    tags=["任务"],
    summary="创建任务（每个课次一个）",
    responses={409: {"description": "已有整理结果，需要 overwrite 确认"}},
    response_model=r.CreateJobsResponse,
)
def create_job(body: CreateJobRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_job(_manager(request), body.payload(), user)


@router.delete("/jobs/{job_id}", tags=["任务"], summary="删除课次任务（移入回收站）", response_model=r.Ok)
def delete_job(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_job(_manager(request), job_id, user)


@router.get(
    "/jobs/{job_id}", tags=["任务"], summary="任务状态；since / after 只返回新的日志与运行输出", response_model=r.Job
)
def get_job(
    job_id: str,
    request: Request,
    user: CurrentUser,
    since: str = Query("0", description="已读取的日志条数"),
    after: str = Query("0", description="已读取的运行输出序号"),
) -> dict[str, Any]:
    job = service.get_job(_manager(request), job_id, user)
    return job.snapshot(_non_negative(since, "since"), _non_negative(after, "after"))


@router.get("/jobs/{job_id}/export/anki", tags=["导出"], response_class=Response)
def export_anki(job_id: str, request: Request, user: CurrentUser) -> Response:
    from ...export import anki_text

    job = service.get_job(_manager(request), job_id, user)
    with job._lock:
        if not job.result or not job.result.note_workspace:
            raise HTTPException(400, detail="课程尚未生成笔记")
        text = anki_text(job.result.note_workspace)
    return Response(
        text,
        media_type="text/tab-separated-values; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(job.title, safe='')}.anki.tsv"},
    )


@router.get("/jobs/{job_id}/events", tags=["任务"], summary="以 SSE 推送任务状态变化", response_class=StreamingResponse)
async def job_events(
    job_id: str, request: Request, user: CurrentUser, since: str = Query("0"), after: str = Query("0")
) -> StreamingResponse:
    job = service.get_job(_manager(request), job_id, user)
    since_value, after_value = _non_negative(since, "since"), _non_negative(after, "after")
    interval = getattr(request.app.state, "event_interval", 0.5)

    async def stream() -> AsyncIterator[bytes]:
        log_offset, event_offset, last = since_value, after_value, None
        while not await request.is_disconnected():
            snapshot = job.snapshot(log_offset, event_offset)
            key = (
                snapshot["status"],
                snapshot["progress"],
                snapshot["message"],
                snapshot["revision"],
                snapshot["log_offset"],
                snapshot["event_offset"],
                len(snapshot["messages"]),
                snapshot["pending_messages"],
            )
            if key != last:
                last = key
                yield f"event: job\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n".encode()
                log_offset, event_offset = snapshot["log_offset"], snapshot["event_offset"]
            if snapshot["status"] in TERMINAL and not snapshot["pending_messages"]:
                yield b"event: end\ndata: {}\n\n"
                return
            await asyncio.sleep(interval)

    return StreamingResponse(
        stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/jobs/{job_id}/file", tags=["产物"], summary="下载任务目录中的文件", response_class=FileResponse)
def job_file(job_id: str, request: Request, user: CurrentUser, rel: str = "", inline: str = "0") -> FileResponse:
    target = service.job_file(service.get_job(_manager(request), job_id, user), rel)
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
    return service.preview(service.get_job(_manager(request), job_id, user), rel)


@router.post("/jobs/{job_id}/messages", tags=["任务"], summary="继续提问或要求修改笔记", response_model=r.JobResponse)
def send_message(job_id: str, body: MessageRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.send_message(_manager(request), job_id, body.payload(), user)


@router.get(
    "/jobs/{job_id}/tasks",
    tags=["任务"],
    summary="各阶段的执行记录：节点、状态、生成模型",
    response_model=r.TasksResponse,
)
def job_tasks(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"tasks": service.job_tasks(_manager(request), job_id, user)}


@router.post(
    "/jobs/{job_id}/rerun",
    tags=["任务"],
    summary="从指定阶段重新运行，之前的阶段复用已有结果",
    response_model=r.JobResponse,
)
def rerun_job(job_id: str, body: RerunRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.rerun_job(_manager(request), job_id, body.payload(), user)


@router.get("/jobs/{job_id}/versions", tags=["笔记"], summary="笔记版本历史", response_model=r.VersionsResponse)
def note_versions(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"versions": service.note_versions(_manager(request), job_id, user)}


@router.get(
    "/jobs/{job_id}/versions/{version}",
    tags=["笔记"],
    summary="某个版本的 Markdown 原文",
    response_model=r.NoteVersionText,
)
def note_version(
    job_id: str, version: int, request: Request, user: CurrentUser, kind: str = "detailed"
) -> dict[str, Any]:
    return service.note_version_text(_manager(request), job_id, version, kind, user)


@router.post(
    "/jobs/{job_id}/versions/{version}/restore",
    tags=["笔记"],
    summary="恢复到某个版本（另存为新版本）",
    response_model=r.JobResponse,
)
def restore_version(job_id: str, version: int, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.restore_note_version(_manager(request), job_id, version, user)


@router.post("/workers", tags=["计算服务"], summary="创建节点令牌；令牌只在本次返回", response_model=r.WorkerToken)
def create_worker(body: WorkerTokenRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_worker_token(_manager(request), body.payload(), user)


@router.delete("/workers/{worker_id}", tags=["计算服务"], summary="停用或永久删除节点并作废其令牌", response_model=r.Ok)
def delete_worker(
    worker_id: str,
    request: Request,
    user: CurrentUser,
    permanent: bool = Query(False, description="永久删除节点记录；默认仅停用。内置节点和仍有未完成任务的节点不能删除"),
) -> dict[str, Any]:
    if permanent:
        return service.delete_worker(_manager(request), worker_id, user)
    return service.disable_worker(_manager(request), worker_id, user)


@router.get("/workers", tags=["计算服务"], summary="计算节点、在线状态与能力", response_model=r.WorkersResponse)
def list_workers(request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"workers": service.list_workers(_manager(request), user)}


@router.get("/hub-links", tags=["计算服务"], summary="本机对外连接的远程 Hub 列表", response_model=r.HubLinksResponse)
def list_hub_links(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_hub_links(_manager(request), user)


@router.post("/hub-links", tags=["计算服务"], summary="添加对外连接的远程 Hub", response_model=r.HubLinkResponse)
def create_hub_link(body: HubLinkCreateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_hub_link(_manager(request), body.payload(), user)


@router.patch(
    "/hub-links/{link_id}", tags=["计算服务"], summary="修改对外连接的远程 Hub 配置", response_model=r.HubLinkResponse
)
def update_hub_link(link_id: str, body: HubLinkUpdateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.update_hub_link(_manager(request), link_id, body.payload(), user)


@router.post(
    "/hub-links/{link_id}/connect", tags=["计算服务"], summary="连接指定的远程 Hub", response_model=r.HubLinkResponse
)
def connect_hub_link(link_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.connect_hub_link(_manager(request), link_id, user)


@router.post(
    "/hub-links/{link_id}/disconnect",
    tags=["计算服务"],
    summary="断开指定的远程 Hub",
    response_model=r.HubLinkResponse,
)
def disconnect_hub_link(link_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.disconnect_hub_link(_manager(request), link_id, user)


@router.delete("/hub-links/{link_id}", tags=["计算服务"], summary="删除对外连接的远程 Hub", response_model=r.Ok)
def delete_hub_link(link_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_hub_link(_manager(request), link_id, user)


@router.post("/jobs/{job_id}/cancel", tags=["任务"], summary="取消任务", response_model=r.Ok)
def cancel_job(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.cancel_job(_manager(request), job_id, user)


@router.get(
    "/jobs/{job_id}/bundle",
    tags=["互传"],
    summary="导出单个课次的便携产物包（.c2m.zip）",
    response_class=FileResponse,
)
def export_job_bundle(job_id: str, request: Request, user: CurrentUser) -> FileResponse:
    path, filename = service.export_job_bundle_file(_manager(request), job_id, user)
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
) -> FileResponse:
    selected = [item.strip() for item in job_ids.split(",") if item.strip()] if job_ids else None
    path, filename = service.export_course_bundle_file(_manager(request), course_id, selected, user)
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
    from ...jobs.bundle import MAX_BUNDLE_BYTES

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
            _manager(request),
            tmp_path,
            user=user,
            conflict=conflict,
            out_dir=out_dir,
        )
    finally:
        tmp_path.unlink(missing_ok=True)


@router.get("/transfer/peers", tags=["互传"], summary="已保存的对端 Hub 列表", response_model=r.TransferPeersResponse)
def list_transfer_peers(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_transfer_peers(_manager(request), user)


@router.post("/transfer/peers", tags=["互传"], summary="保存一个对端 Hub", response_model=r.TransferPeerResponse)
def create_transfer_peer(body: TransferPeerCreateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_transfer_peer(_manager(request), body.payload(), user)


@router.patch(
    "/transfer/peers/{peer_id}", tags=["互传"], summary="修改已保存的对端 Hub", response_model=r.TransferPeerResponse
)
def update_transfer_peer(
    peer_id: str, body: TransferPeerUpdateRequest, request: Request, user: CurrentUser
) -> dict[str, Any]:
    return service.update_transfer_peer(_manager(request), peer_id, body.payload(), user)


@router.delete("/transfer/peers/{peer_id}", tags=["互传"], summary="删除已保存的对端 Hub", response_model=r.Ok)
def delete_transfer_peer(peer_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_transfer_peer(_manager(request), peer_id, user)


@router.post(
    "/transfer/remote",
    tags=["互传"],
    summary="连接对端 Hub 并列出可拉取的课程与课次",
    response_model=r.RemoteCatalogResponse,
)
def remote_catalog(body: RemoteCatalogRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.remote_catalog(_manager(request), body.payload(), user)


@router.post(
    "/transfer/push",
    tags=["互传"],
    summary="将本 Hub 的课程或课次产物推送到对端 Hub",
    response_model=r.TransferResultResponse,
)
def transfer_push(body: TransferPushRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.transfer_push(_manager(request), body.payload(), user)


@router.post(
    "/transfer/pull",
    tags=["互传"],
    summary="从对端 Hub 拉取课程或课次产物并导入本 Hub",
    response_model=r.TransferResultResponse,
)
def transfer_pull(body: TransferPullRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.transfer_pull(_manager(request), body.payload(), user)


def _non_negative(value: str, name: str) -> int:
    if not value.isdecimal() or len(value) > 15:
        raise ValueError(f"参数 {name} 必须是非负整数")
    return int(value)

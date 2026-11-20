"""计算节点接口：/api/v1/worker/*，使用节点令牌（Authorization: Bearer）鉴权。

节点主动连接 Hub：注册能力、领取任务、续约、回报进度、上传产物文件、提交结果。
路径在 Hub 任务目录与 ``job://`` 之间转换（见 :mod:`course2md.util.job_refs`）。
"""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ...core.queue import LeaseLost
from ...storage import file_digest
from ...util.job_refs import from_job_refs, job_refs, resolve_relative, to_job_refs

router = APIRouter(prefix="/worker", tags=["计算节点"])
MAX_BLOB_BYTES = 2 * 1024 * 1024 * 1024


class Unauthorized(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=401, detail="节点令牌无效或已停用")


def _queue(request: Request):
    queue = getattr(request.app.state.manager, "queue", None)
    if queue is None:
        raise HTTPException(status_code=503, detail="Hub 未启用任务队列")
    return queue


def current_worker(request: Request, authorization: str = Header("")) -> dict[str, Any]:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        raise Unauthorized()
    worker = _queue(request).authenticate_worker(token.strip())
    if worker is None:
        raise Unauthorized()
    return worker


def _owned_task(request: Request, worker: dict[str, Any], task_id: str) -> dict[str, Any]:
    task = _queue(request).get(task_id)
    if (
        task is None
        or task["worker_id"] != worker["id"]
        or task["status"] != "leased"
        or (task["lease_until"] or 0) < time.time()
    ):
        raise HTTPException(status_code=409, detail="任务不属于该节点或租约已失效")
    return task


def _job_root(request: Request, task_id: str) -> Path:
    with _queue(request).db.session() as session:
        from ...core.models import TaskRow

        row = session.get(TaskRow, task_id)
        return Path(row.payload["options"]["out_dir"])


class RegisterBody(BaseModel):
    name: str = ""
    capabilities: list[str] = Field(default_factory=list)
    version: str = ""


class ClaimBody(BaseModel):
    capabilities: list[str] = Field(default_factory=list)
    lease_seconds: float = Field(60.0, ge=5, le=3600)


class RenewBody(BaseModel):
    lease_seconds: float = Field(60.0, ge=5, le=3600)


class ProgressBody(BaseModel):
    stage: str = ""
    fraction: float = 0.0
    message: str = ""
    sub: float | None = None


class CompleteBody(BaseModel):
    output: Any = None
    warnings: list[str] = Field(default_factory=list)
    model: str = ""
    files: dict[str, str] = Field(default_factory=dict, description="任务目录内相对路径 → 内容摘要")


class FailBody(BaseModel):
    error: str = ""
    error_type: str = ""
    cancelled: bool = False
    warnings: list[str] = Field(default_factory=list)


@router.post("/register", summary="注册或更新节点能力")
def register(body: RegisterBody, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _queue(request).register_worker(
        worker["id"], body.name or worker["name"], body.capabilities, kind="remote", version=body.version
    )
    return {"worker_id": worker["id"]}


@router.post("/heartbeat", summary="节点在线心跳")
def heartbeat(request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _queue(request).touch_worker(worker["id"])
    return {"ok": True}


@router.post("/leave", summary="节点正常退出")
def leave(request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _queue(request).leave(worker["id"])
    return {"ok": True}


@router.post("/claim", summary="领取一个能力满足的任务；没有任务时 task 为 null")
def claim(body: ClaimBody, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    queue = _queue(request)
    queue.touch_worker(worker["id"])
    task = queue.claim(worker["id"], set(body.capabilities), body.lease_seconds)
    if task is None:
        return {"task": None}
    root = Path(task.payload["options"]["out_dir"])
    payload = to_job_refs(task.payload, root)
    input_files: list[str] = []
    for relative in set(job_refs(payload["inputs"])):
        target = resolve_relative(root, relative)
        candidates = target.rglob("*") if target.is_dir() else [target]
        for path in candidates:
            if (
                path.is_file()
                and path.resolve().is_relative_to(root.resolve())
                and path.relative_to(root).parts[0] not in ("media", "audio")
                and not path.name.endswith((".part", ".lock"))
            ):
                input_files.append(path.relative_to(root).as_posix())
    payload["input_files"] = sorted(set(input_files))
    return {
        "task": {
            "id": task.id,
            "job_id": task.job_id,
            "stage": task.stage,
            "attempts": task.attempts,
            "lease_until": task.lease_until,
            "payload": payload,
        }
    }


@router.post("/tasks/{task_id}/renew", summary="续约；返回是否已请求取消")
def renew(task_id: str, body: RenewBody, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    try:
        return {"cancel": _queue(request).renew(task_id, worker["id"], body.lease_seconds)}
    except LeaseLost as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/tasks/{task_id}/progress", summary="回报进度")
def progress(task_id: str, body: ProgressBody, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _queue(request).progress(task_id, worker["id"], body.stage, body.fraction, body.message, body.sub)
    return {"ok": True}


@router.post("/tasks/{task_id}/release", summary="节点停机时归还任务，供重启后接续")
def release(task_id: str, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _owned_task(request, worker, task_id)
    _queue(request).release(task_id, worker["id"])
    return {"ok": True}


@router.post("/tasks/{task_id}/events", summary="回报运行输出")
def events(task_id: str, body: dict[str, Any], request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _owned_task(request, worker, task_id)
    _queue(request).add_event(task_id, body)
    return {"ok": True}


@router.get("/tasks/{task_id}/files", summary="下载任务目录中的输入文件", response_class=FileResponse)
def task_file(task_id: str, rel: str, request: Request, worker=Depends(current_worker)) -> Response:
    _owned_task(request, worker, task_id)
    root = _job_root(request, task_id)
    try:
        target = resolve_relative(root, rel).resolve()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not target.is_relative_to(root.resolve()) or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    digest = file_digest(target)
    headers = {"ETag": f'"{digest}"', "X-Content-SHA256": digest}
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return FileResponse(target, media_type="application/octet-stream", headers=headers)


@router.head("/blobs/{digest}", summary="检查产物文件是否已上传")
def blob_exists(digest: str, request: Request, worker=Depends(current_worker)) -> Response:
    try:
        exists = _queue(request).store.exists(digest)
    except ValueError:
        exists = False
    return Response(status_code=200 if exists else 404)


@router.put("/blobs/{digest}", summary="上传产物文件（按内容摘要校验）")
async def upload_blob(digest: str, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    store = _queue(request).store
    received = 0
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as buffer:
        async for chunk in request.stream():
            received += len(chunk)
            if received > MAX_BLOB_BYTES:
                raise HTTPException(status_code=413, detail="文件过大")
            buffer.write(chunk)
        buffer.seek(0)
        actual = store.put_stream(buffer)
    if actual != digest:
        raise HTTPException(status_code=400, detail="文件内容与摘要不符")
    return {"digest": actual}


@router.post("/tasks/{task_id}/complete", summary="提交输出产物；files 中的文件写回 Hub 任务目录")
def complete(task_id: str, body: CompleteBody, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    queue = _queue(request)
    _owned_task(request, worker, task_id)
    root = _job_root(request, task_id)
    targets = {}
    for relative, digest in body.files.items():
        try:
            target = resolve_relative(root, relative)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not queue.store.exists(digest):
            raise HTTPException(status_code=400, detail=f"文件 {relative} 尚未上传")
        targets[target] = digest
    root.parent.mkdir(parents=True, exist_ok=True)
    # 大文件先写到课程目录之外，期间仍可续约；拿到任务行锁后只做快速改名。
    with tempfile.TemporaryDirectory(prefix=".course2md-publish-", dir=root.parent) as directory:
        staged = {}
        for index, (target, digest) in enumerate(targets.items()):
            staged[target] = queue.store.copy_to(digest, Path(directory) / str(index))

        def publish() -> None:
            for target, temporary in staged.items():
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary.replace(target)

        try:
            digest = queue.complete(
                task_id, worker["id"], from_job_refs(body.output, root), body.warnings, body.model, publish=publish
            )
        except LeaseLost as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"result": digest}


@router.post("/tasks/{task_id}/fail", summary="报告失败或已取消")
def fail(task_id: str, body: FailBody, request: Request, worker=Depends(current_worker)) -> dict[str, Any]:
    _queue(request).fail(
        task_id, worker["id"], body.error, body.error_type, cancelled=body.cancelled, warnings=body.warnings
    )
    return {"ok": True}

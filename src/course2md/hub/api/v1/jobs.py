"""课次任务的创建、状态、事件流、对话、取消与重跑。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from ... import responses as r
from ... import service
from ...schemas import (
    CreateJobRequest,
    MessageRequest,
    RerunRequest,
)
from .deps import CurrentUser, get_manager, non_negative

router = APIRouter()
TERMINAL = ("done", "failed", "cancelled")


@router.get("/jobs", tags=["任务"], summary="全部任务", response_model=r.JobsResponse)
def list_jobs(request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"jobs": service.list_jobs(get_manager(request), user)}


@router.post(
    "/jobs",
    tags=["任务"],
    summary="创建任务（每个课次一个）",
    responses={409: {"description": "已有整理结果，需要 overwrite 确认"}},
    response_model=r.CreateJobsResponse,
)
def create_job(body: CreateJobRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_job(get_manager(request), body.payload(), user)


@router.delete("/jobs/{job_id}", tags=["任务"], summary="删除课次任务（移入回收站）", response_model=r.Ok)
def delete_job(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_job(get_manager(request), job_id, user)


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
    job = service.get_job(get_manager(request), job_id, user)
    return job.snapshot(non_negative(since, "since"), non_negative(after, "after"))


@router.get("/jobs/{job_id}/events", tags=["任务"], summary="以 SSE 推送任务状态变化", response_class=StreamingResponse)
async def job_events(
    job_id: str, request: Request, user: CurrentUser, since: str = Query("0"), after: str = Query("0")
) -> StreamingResponse:
    job = service.get_job(get_manager(request), job_id, user)
    since_value, after_value = non_negative(since, "since"), non_negative(after, "after")
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


@router.post("/jobs/{job_id}/messages", tags=["任务"], summary="继续提问或要求修改笔记", response_model=r.JobResponse)
def send_message(job_id: str, body: MessageRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.send_message(get_manager(request), job_id, body.payload(), user)


@router.get(
    "/jobs/{job_id}/tasks",
    tags=["任务"],
    summary="各阶段的执行记录：节点、状态、生成模型",
    response_model=r.TasksResponse,
)
def job_tasks(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"tasks": service.job_tasks(get_manager(request), job_id, user)}


@router.post(
    "/jobs/{job_id}/rerun",
    tags=["任务"],
    summary="从指定阶段重新运行，之前的阶段复用已有结果",
    response_model=r.JobResponse,
)
def rerun_job(job_id: str, body: RerunRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.rerun_job(get_manager(request), job_id, body.payload(), user)


@router.post("/jobs/{job_id}/cancel", tags=["任务"], summary="取消任务", response_model=r.Ok)
def cancel_job(job_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.cancel_job(get_manager(request), job_id, user)

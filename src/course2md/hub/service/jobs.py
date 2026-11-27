"""任务与课程的查询、预览、对话、取消与重跑。"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import replace
from urllib.parse import quote, unquote, urlparse

from markdown_it import MarkdownIt

from ...config import (
    default_agent,
)
from ...core.accounts import LOCAL_USER, User
from ...jobs.job import ACTIVE_STATUSES, Job
from ...jobs.manager import JobManager
from ...jobs.records import CourseView
from ...stages.registry import PIPELINE_STAGES, STAGE_LABELS
from ..access import Forbidden, NotFound, server_mode
from ..plugins import ensure_ready
from .common import get_job, job_file, request_cancel, visible


def list_jobs(manager: JobManager, user: User = LOCAL_USER) -> list[dict]:
    with manager.locked():
        jobs = [job for job in manager.jobs.values() if visible(user, job.owner) and job.deleted_at is None]
    return [job.snapshot(0) for job in jobs]


def list_courses(manager: JobManager, user: User = LOCAL_USER) -> list[CourseView]:
    with manager.locked():
        courses = [
            course
            for course in manager.courses.values()
            if visible(user, course.get("owner")) and not course.get("deleted_at")
        ]
    return [manager.course_snapshot(course) for course in courses]


def preview(job: Job, rel: str) -> dict:
    target = job_file(job, rel)
    if target.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        relative = quote(target.relative_to(job.out_dir.resolve()).as_posix(), safe="")
        return {"name": target.name, "image_url": f"/api/v1/jobs/{job.id}/file?rel={relative}&inline=1"}
    text = target.read_text(encoding="utf-8", errors="replace")[:200_000]
    payload = {"name": target.name, "text": text}
    if target.suffix.lower() == ".md":
        markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
        tokens = markdown.parse(text)
        for block in tokens:
            for token in block.children or []:
                if token.type not in ("image", "link_open"):
                    continue
                attribute = "src" if token.type == "image" else "href"
                address = urlparse(str(token.attrGet(attribute) or ""))
                if address.scheme or address.netloc:
                    if token.type == "image":
                        token.type, token.content = "text", f"[图片链接 {token.content}]"
                    continue
                linked = (target.parent / unquote(address.path)).resolve()
                if not linked.is_relative_to(job.out_dir.resolve()) or not linked.is_file():
                    if token.type == "image":
                        token.type, token.content = "text", f"[图片不可用 {token.content}]"
                    continue
                relative = linked.relative_to(job.out_dir.resolve()).as_posix()
                if token.type == "image":
                    if linked.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
                        token.type, token.content = "text", f"[图片格式不支持 {token.content}]"
                        continue
                    token.attrSet("src", f"/api/v1/jobs/{job.id}/file?rel={quote(relative, safe='')}&inline=1")
                elif linked.suffix.lower() == ".md":
                    token.attrSet("href", "#")
                    token.attrSet("data-note-rel", relative)
        payload["html"] = markdown.renderer.render(tokens, markdown.options, {})
    return payload


def send_message(manager: JobManager, job_id: str, body: dict, user: User = LOCAL_USER) -> dict:
    job = manager.get(job_id)
    if job is None or not visible(user, job.owner):
        raise NotFound("课程不存在")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("请填写要提问或修改的内容")
    request_id = body.get("id") or uuid.uuid4().hex
    if not isinstance(request_id, str) or not re.fullmatch(r"[\w-]{1,100}", request_id):
        raise ValueError("请求编号无效，请重新发送")
    manager.send(job, text.strip(), request_id)
    return {"job": job.snapshot()}


def cancel_job(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> dict:
    job = get_job(manager, job_id, user)
    request_cancel(manager, job)
    job.log("收到取消请求")
    manager.save_job(job)
    return {"ok": True}


def job_tasks(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> list[dict]:
    """任务各阶段的执行记录（节点、状态、耗时、生成模型），不含选项与输入。"""
    get_job(manager, job_id, user)
    queue = getattr(manager, "queue", None)
    if queue is None:
        return []
    hidden = ("payload", "cache_key")
    return [{key: value for key, value in task.items() if key not in hidden} for task in queue.tasks_for_job(job_id)]


STAGE_ORDER = PIPELINE_STAGES


def rerun_job(manager: JobManager, job_id: str, body: dict, user: User = LOCAL_USER) -> dict:
    """从指定阶段重新运行，之前阶段在输入与设置不变时复用已有结果。可换整理后端与模型。"""
    ensure_ready()
    job = get_job(manager, job_id, user)
    from_stage = body.get("from_stage") or "organize"
    if from_stage not in STAGE_ORDER:
        raise ValueError(f"from_stage 可选 {', '.join(STAGE_ORDER)}")
    with manager.locked():
        if job.status in ACTIVE_STATUSES:
            raise ValueError(f"「{job.title}」正在整理中，请等待当前任务完成")
        if job.options is None:
            raise ValueError("这个任务没有保存运行设置，请重新创建任务")
        options = job.options
        if any(key in body for key in ("agent", "agent_provider", "agent_model")):
            from ...agents import AgentError

            provider, model = body.get("agent_provider"), body.get("agent_model")
            if not all(value is None or isinstance(value, str) for value in (provider, model)):
                raise ValueError("Agent 提供商与模型必须是文字")
            try:
                agent = default_agent(body.get("agent", options.agent.preset or "none"), provider=provider, model=model)
            except AgentError as exc:
                raise ValueError(str(exc)) from None
            if server_mode(manager) and not user.is_admin and agent.command:
                raise Forbidden("服务器上不能运行自定义整理命令，请选择 dsh、codex 或内置整理")
            if agent.preset and not agent.resolved_command() and not server_mode(manager):
                raise ValueError(f"找不到 {agent.preset}，请安装对应 CLI 或检查启动路径配置")
            options = replace(options, agent=agent)
            job.agent = agent.preset or ("custom" if agent.command else "builtin")
            job.agent_model = agent.label
        if body.get("llm_model"):
            options = replace(options, llm=replace(options.llm, model=str(body["llm_model"])))
        if "detail" in body:
            options = replace(options, detail=bool(body["detail"]))
        job.reuse = frozenset(STAGE_ORDER[: STAGE_ORDER.index(from_stage)])
        job.cancel = threading.Event()
        job.status, job.error, job.progress, job.stage = "queued", "", 0.0, "排队"
        job.message = f"从「{STAGE_LABELS[from_stage]}」重新运行"
        manager.submit(job, options)
        job.log(job.message)
    return {"job": job.snapshot(0)}

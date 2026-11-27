"""创建任务：把解析出的课次展开为任务并提交。"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ...config import (
    AsrConfig,
    LlmConfig,
    default_agent,
    default_delete_video,
    default_detail,
    default_formats,
    default_frame_interval,
    default_frames,
    default_term_start,
)
from ...core.accounts import LOCAL_USER, User
from ...jobs.job import ACTIVE_STATUSES, ConfirmationRequired, Job
from ...jobs.manager import FORMATS, JobManager
from ...jobs.sessions import describe_sessions, existing_result, resolver_fields, session_folder
from ...resolvers import (
    ResolverError,
    get_resolver,
    parse_date,
    public_source,
    resolve_term_start,
    week_of,
)
from ...resolvers.base import SessionInfo
from ...stages.options import JobOptions
from ...util.text import safe_name
from .common import Forbidden, get_course, output_root, server_mode
from .server_config import resolve_frame_selector
from .sources import check_source, list_sessions, stored_sessions


def create_targets(manager: JobManager, body: dict, source: str, root: Path, user: User = LOCAL_USER) -> list[dict]:  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
    """把请求里的课次展开成「一个课次一个任务」的目标列表。"""
    wanted = body.get("session_ids")
    if isinstance(wanted, list):
        wanted = [str(item).strip() for item in wanted if str(item).strip()]
    else:
        wanted = []
    if not wanted and body.get("session_id"):
        wanted = [str(body["session_id"])]
    wanted = list(dict.fromkeys(wanted))

    sessions: list[SessionInfo] = []
    resolver_name = ""
    failure: Exception | None = None
    try:
        resolver = get_resolver(source)
        resolver_name = resolver.name
        check_source(manager, user, resolver_name)
        sessions = (
            stored_sessions(manager, body, source, user, wanted) if server_mode(manager) else []
        ) or list_sessions(manager, source, resolver, user)
    except (ResolverError, OSError) as exc:
        failure = exc

    hint = next((session.meta.get("term_start") for session in sessions if session.meta.get("term_start")), "")
    term_start, _ = resolve_term_start(
        body.get("term_start") or default_term_start(), hint, [session.date for session in sessions]
    )

    if wanted:
        if not sessions:
            raise failure or ResolverError("没有查到任何课次，请重新解析来源")
        known = {session.session_id: session for session in sessions}
        missing = [session_id for session_id in wanted if session_id not in known]
        if missing:
            raise ResolverError("课次列表已更新，请重新解析来源后再试")
        picked = [known[session_id] for session_id in wanted]
    elif sessions:
        picked = [sessions[0]]
    else:
        picked = []

    targets: list[dict[str, Any]] = []
    for session in picked:
        day = parse_date(session.date)
        week = week_of(day, term_start) if day and term_start else None
        course = session.course or (body.get("course") or "").strip()
        folder = root / safe_name(course, "未分类") / safe_name(session_folder(session, course), "session")
        targets.append(
            {
                "id": uuid.uuid4().hex[:12],
                "session_id": session.session_id,
                "title": session.title,
                "course": course,
                "date": session.date,
                "week": week,
                "folder": folder,
            }
        )
    if not targets:
        title = body.get("title") or safe_name(Path(source.split("?", maxsplit=1)[0]).stem or "course")
        targets.append(
            {
                "id": uuid.uuid4().hex[:12],
                "session_id": None,
                "title": title,
                "course": (body.get("course") or "").strip(),
                "date": "",
                "week": None,
                "folder": None,
            }
        )
    for target in targets:
        if target["folder"] is None:
            target["folder"] = root / f"{safe_name(target['title'])}-{target['id']}"
    folders = [target["folder"] for target in targets]
    if len(set(folders)) != len(folders):
        raise ValueError("选中的课次指向同一个输出目录，请只保留一个")
    resolved = {
        **describe_sessions(sessions, root, body.get("term_start", "")),
        **(resolver_fields(resolver, source) if resolver_name else {"resolver": ""}),
    }
    if not resolved["course"]:
        resolved["course"] = targets[0]["course"] or targets[0]["title"]
    course = manager.remember_course(source, root, resolved, body.get("course_id", ""), owner=user.id)
    for target in targets:
        target["course_id"] = course["id"]
    return targets


def _create_job_locked(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
    from ..plugins import ensure_ready

    ensure_ready()
    source = (body.get("source") or "").strip()
    if not source:
        raise ValueError("请先填写链接或路径")
    asr_env, llm_env = AsrConfig.from_env(), LlmConfig.from_env()
    root = output_root(manager, user, body.get("out_dir"))
    restricted = server_mode(manager) and not user.is_admin
    if restricted and body.get("llm_base") and str(body["llm_base"]).strip() != llm_env.base_url:
        raise Forbidden("服务器上只能使用管理员配置的大模型接口")
    if body.get("course_id") and body["course_id"] in manager.courses:
        get_course(manager, body["course_id"], user)
    formats = body.get("formats") or default_formats()
    if not isinstance(formats, (list, tuple)):
        raise ValueError("输出格式必须是列表")
    formats = tuple(dict.fromkeys(str(item).strip() for item in formats))
    unknown = [item for item in formats if item not in FORMATS]
    if unknown:
        raise ValueError(f"不支持的输出格式：{', '.join(unknown)}，可选 {', '.join(FORMATS)}")
    try:
        frame_interval = float(body.get("frame_interval", default_frame_interval()))
    except (TypeError, ValueError):
        raise ValueError("画面采样间隔必须是数字") from None
    if not 0 < frame_interval < float("inf"):
        raise ValueError("画面采样间隔必须为大于 0 的有限秒数")
    llm_base = str(body.get("llm_base") or llm_env.base_url).strip()
    address = urlparse(llm_base)
    if address.scheme not in ("http", "https") or not address.netloc:
        raise ValueError("大模型接口地址必须是 http(s) 链接")
    device = body.get("device") or asr_env.device
    if device not in ("cuda", "cpu"):
        raise ValueError("转写设备只能是 cuda 或 cpu")
    asr = replace(
        asr_env,
        model=body.get("model") or asr_env.model,
        device=device,
        compute_type=body.get("compute_type")
        or (asr_env.compute_type if device == asr_env.device else ("int8_float16" if device == "cuda" else "int8")),
    )
    from ...agents import AgentError

    provider, model = body.get("agent_provider"), body.get("agent_model")
    if not all(value is None or isinstance(value, str) for value in (provider, model)):
        raise ValueError("Agent 提供商与模型必须是文字")
    try:
        agent = default_agent(body.get("agent"), provider=provider, model=model)
    except AgentError as exc:
        raise ValueError(str(exc)) from None
    if restricted and agent.command:
        raise Forbidden("服务器上不能运行自定义整理命令，请选择 dsh、codex 或内置整理")
    if agent.preset and not agent.resolved_command() and not server_mode(manager):
        # 服务器上 Agent 通常装在计算节点而不是 Hub，交给有 agent:<名称> 能力的节点执行
        raise ValueError(f"找不到 {agent.preset}，请安装对应 CLI 或检查启动路径配置")

    frame_selector = resolve_frame_selector(manager, body.get("frame_selector"))

    views = body.get("views") or None
    if views:
        if not isinstance(views, list) or not all(str(v).isdecimal() and int(v) > 0 for v in views):
            raise ValueError("机位编号必须是正整数列表")
        views = [int(v) for v in views]

    targets = create_targets(manager, body, source, root, user)
    for target in targets:
        for current in manager.jobs.values():
            if current.out_dir == target["folder"] and current.status in ACTIVE_STATUSES:
                raise ValueError(f"「{current.title}」正在整理中，请等待当前任务完成")

    existing = []
    for target in targets:
        found = existing_result(target["folder"])
        if found:
            existing.append(
                {
                    **found,
                    "session_id": target["session_id"] or "",
                    "title": target["title"],
                    "date": target["date"],
                    "week": target["week"],
                }
            )
    if existing and not body.get("overwrite"):
        raise ConfirmationRequired(
            {
                "error": f"有 {len(existing)} 个课次已存在整理结果，确认后才会覆盖",
                "requires_confirmation": True,
                "existing": existing,
                "selected": [
                    {"session_id": target["session_id"] or "", "title": target["title"]} for target in targets
                ],
            }
        )

    for target in targets:
        for current in list(manager.jobs.values()):
            if current.out_dir == target["folder"] and current.status not in ACTIVE_STATUSES:
                manager.jobs.pop(current.id, None)

    jobs: list[Job] = []
    snapshots: list[dict] = []
    for target in targets:
        job = Job(
            id=target["id"],
            source=source,
            title=target["title"],
            out_dir=target["folder"],
            course=target["course"],
            course_id=target["course_id"],
            session_id=target["session_id"] or "",
            session_date=target["date"],
            week=target["week"],
            agent=agent.preset or ("custom" if agent.command else "builtin"),
            agent_model=agent.label,
            owner=user.id,
        )
        options = JobOptions(
            source=source,
            out_dir=job.out_dir,
            formats=formats,
            session_id=target["session_id"],
            views=views,
            course=target["course"],
            session_date=target["date"],
            week=target["week"],
            ai=bool(body.get("ai", body.get("refine", True))),
            proofread=bool(body.get("proofread", True)),
            detail=bool(body.get("detail", default_detail())),
            frames=bool(body.get("frames", default_frames())),
            frame_interval=frame_interval,
            frame_selector=frame_selector,
            asr=asr,
            llm=replace(llm_env, model=body.get("llm_model") or llm_env.model, base_url=llm_base),
            agent=agent,
            keep_audio=bool(body.get("keep_audio")),
            delete_video=bool(body.get("delete_video", default_delete_video())),
        )
        manager.submit(job, options)
        job.log(f"任务已提交：{public_source(source)[:100]}")
        jobs.append(job)
        snapshots.append(job.snapshot(0))
    course = manager.courses[jobs[0].course_id]
    return {"jobs": snapshots, "id": jobs[0].id, "job": snapshots[0], "course": manager.course_snapshot(course)}


def create_job(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:
    with manager.locked():
        return _create_job_locked(manager, body, user)

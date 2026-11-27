"""Hub 的业务逻辑：配置、来源解析、创建任务、文件预览。

与 HTTP 框架无关，接口层（:mod:`course2md.hub.api`）只负责解析请求与返回结果。
校验失败抛出 ValueError / ResolverError（400），需要用户确认时抛出 ConfirmationRequired（409），
资源不存在时抛出 NotFound（404）。
"""

from __future__ import annotations

import os
import platform
import re
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from markdown_it import MarkdownIt

from ..config import (
    AsrConfig,
    LlmConfig,
    default_agent,
    default_delete_video,
    default_detail,
    default_formats,
    default_frame_interval,
    default_frame_selector,
    default_frames,
    default_out_dir,
    default_term_start,
    env,
    find_model,
)
from ..core.accounts import LOCAL_USER, User
from ..errors import Course2mdError
from ..jobs.manager import (
    ACTIVE_STATUSES,
    FORMATS,
    TRASH_SECONDS,
    ConfirmationRequired,
    Job,
    JobManager,
    _describe_sessions,
    _existing_result,
    _resolver_fields,
    _session_folder,
)
from ..resolvers import (
    ResolverError,
    available,
    get_resolver,
    parse_date,
    public_source,
    resolve_term_start,
    week_of,
)
from ..resolvers.base import SessionInfo
from ..stages.codec import decode, options_to_dict
from ..stages.executor import rebuild_task_error
from ..stages.options import JobOptions
from ..stages.registry import PIPELINE_STAGES, STAGE_LABELS
from ..stages.sessions import SessionsStage
from ..util.text import safe_name

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")


class NotFound(LookupError):
    """请求的任务、课程或文件不存在（包括不属于当前用户的）。"""


class Forbidden(Exception):
    """当前用户无权执行该操作。

    不继承 PermissionError：它是 OSError 的子类，会被处理文件错误的 except 子句误吞。
    """


def server_mode(manager: JobManager) -> bool:
    return getattr(manager, "mode", "local") == "server"


def visible(user: User, owner: str | None) -> bool:
    """记录是否属于该用户（本地模式下全部属于本机用户 local）。"""
    return owner == user.id


def output_root(manager: JobManager, user: User, requested: str | None) -> Path:
    """服务器上每个用户一个目录，忽略请求中的路径；本地模式沿用请求或配置中的目录。"""
    base = Path(default_out_dir()).expanduser().resolve()
    if server_mode(manager):
        return base / "users" / user.id
    return Path(requested or base).expanduser().resolve()


def has_local_node(manager: JobManager) -> bool:
    """是否存在与 Hub 同机且可读取本机文件的在线节点（本地应用或 server --with-node）。"""
    queue = getattr(manager, "queue", None)
    if queue is not None:
        return queue.capable_worker_online(["resolver:local", "hub"])
    return getattr(manager, "builtin_worker", None) is not None


def _check_source(manager: JobManager, user: User, resolver_name: str) -> None:
    if resolver_name != "local":
        return
    if server_mode(manager) and not user.is_admin:
        raise Forbidden("服务器上不能读取本地文件，请使用课程平台链接，或在自己的电脑上运行本地应用")
    if server_mode(manager) and not has_local_node(manager):
        raise ResolverError(
            "当前服务器以纯 Hub 模式运行（未启用本机节点），不能读取服务器本地文件；"
            "请使用课程平台链接，或以 course2md server --with-node 启动"
        )


def get_job(manager: JobManager, job_id: str, user: User = LOCAL_USER, include_deleted: bool = False) -> Job:
    job = manager.get(job_id)
    if job is None or not visible(user, job.owner) or (job.deleted_at is not None and not include_deleted):
        raise NotFound("任务不存在")
    return job


def get_course(manager: JobManager, course_id: str, user: User = LOCAL_USER, include_deleted: bool = False) -> dict:
    course = manager.courses.get(course_id)
    if course is None or not visible(user, course.get("owner")) or (course.get("deleted_at") and not include_deleted):
        raise NotFound("课程不存在")
    return course


def list_jobs(manager: JobManager, user: User = LOCAL_USER) -> list[dict]:
    with manager.locked():
        jobs = [job for job in manager.jobs.values() if visible(user, job.owner) and job.deleted_at is None]
    return [job.snapshot(0) for job in jobs]


def list_courses(manager: JobManager, user: User = LOCAL_USER) -> list[dict]:
    with manager.locked():
        courses = [
            course
            for course in manager.courses.values()
            if visible(user, course.get("owner")) and not course.get("deleted_at")
        ]
    return [manager.course_snapshot(course) for course in courses]


# ---------------------------------------------------------------- 回收站
def _request_cancel(manager: JobManager, job: Job) -> None:
    job.cancel.set()
    if getattr(manager, "queue", None):
        for task in manager.queue.tasks_for_job(job.id):
            manager.queue.cancel(task["id"])


def delete_course(manager: JobManager, course_id: str, user: User = LOCAL_USER) -> dict:
    """移入回收站：课程及其全部课次立即从列表消失，进行中的任务先取消；30 天内可恢复。"""
    course = get_course(manager, course_id, user)
    now = time.time()
    for job in manager.course_jobs(course):
        if job.deleted_at is not None:
            continue
        if job.status in ACTIVE_STATUSES:
            _request_cancel(manager, job)
            job.log("课程已删除，任务已取消")
        manager.mark_job_deleted(job, now)
    manager.mark_course_deleted(course, now)
    return {"ok": True}


def delete_job(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> dict:
    job = get_job(manager, job_id, user)
    if job.status in ACTIVE_STATUSES:
        _request_cancel(manager, job)
        job.log("任务已删除，已取消")
    manager.mark_job_deleted(job, time.time())
    return {"ok": True}


def list_trash(manager: JobManager, user: User = LOCAL_USER) -> dict:
    with manager.locked():
        courses = [c for c in manager.courses.values() if c.get("deleted_at") and visible(user, c.get("owner"))]
        jobs = [j for j in manager.jobs.values() if j.deleted_at is not None and visible(user, j.owner)]
    hidden = {course["id"] for course in courses}
    items = [
        {
            "kind": "course",
            "id": course["id"],
            "title": course.get("title") or "课程",
            "deleted_at": course["deleted_at"],
            "purge_at": course["deleted_at"] + TRASH_SECONDS,
            "job_count": sum(1 for job in jobs if job.course_id == course["id"]),
        }
        for course in courses
    ]
    # 所属课程仍在列表中的单个课次，单独出现在回收站
    items += [
        {
            "kind": "job",
            "id": job.id,
            "title": job.title or job.course or "课次",
            "course": job.course,
            "deleted_at": job.deleted_at,
            "purge_at": (job.deleted_at or 0) + TRASH_SECONDS,
            "job_count": 1,
        }
        for job in jobs
        if job.course_id not in hidden
    ]
    items.sort(key=lambda item: item["deleted_at"], reverse=True)
    return {
        "items": items,
        "retention_days": TRASH_SECONDS // 86400,
        # 本地应用的输出目录是用户自己的，永久删除只清理记录
        "removes_files": server_mode(manager),
    }


def _trashed(manager: JobManager, kind: str, item_id: str, user: User) -> Job | dict:
    if kind == "course":
        course = get_course(manager, item_id, user, include_deleted=True)
        if not course.get("deleted_at"):
            raise NotFound("回收站中没有这个课程")
        return course
    if kind == "job":
        job = get_job(manager, item_id, user, include_deleted=True)
        if job.deleted_at is None:
            raise NotFound("回收站中没有这个课次")
        return job
    raise NotFound("未知的回收站项目")


def restore_trash(manager: JobManager, kind: str, item_id: str, user: User = LOCAL_USER) -> dict:
    item = _trashed(manager, kind, item_id, user)
    if isinstance(item, dict):
        manager.mark_course_deleted(item, None)
        for job in manager.course_jobs(item):
            if job.deleted_at is not None:
                manager.mark_job_deleted(job, None)
    else:
        manager.mark_job_deleted(item, None)
    return {"ok": True}


def purge_trash(manager: JobManager, kind: str, item_id: str, user: User = LOCAL_USER) -> dict:
    """立即永久删除，不再等待保留期。"""
    item = _trashed(manager, kind, item_id, user)
    if isinstance(item, dict):
        manager.purge_course(item)
    else:
        manager.purge_job(item)
    return {"ok": True}


def refresh_course(manager: JobManager, course_id: str, body: dict, user: User = LOCAL_USER) -> dict:
    course = get_course(manager, course_id, user)
    return resolve_source(
        manager,
        {
            "source": course["source"],
            "out_dir": course["out_dir"],
            "term_start": course.get("term_start", ""),
            **body,
            "course_id": course_id,
        },
        user,
    )


def create_job(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:
    with manager.locked():
        return _create_job_locked(manager, body, user)


def job_file(job: Job, rel: str) -> Path:
    """任务目录内的文件；越界或不存在时抛出 NotFound。"""
    target = (job.out_dir / unquote(rel)).resolve()
    if not target.is_relative_to(job.out_dir.resolve()) or not target.is_file():
        raise NotFound("文件不存在")
    return target


def _node_capabilities(manager: JobManager, user: User) -> list[tuple[str, set[str]]]:
    """能为该用户领取任务的在线节点及其能力，供网页列出“整个计算服务”实际能做的事。"""
    queue = getattr(manager, "queue", None)
    if queue is None:
        return []
    return [
        (item["name"] or item["id"], set(item["capabilities"]))
        for item in queue.workers()
        if item["online"] and item["owner_id"] in (None, user.id)
    ]


def available_agents(manager: JobManager, user: User = LOCAL_USER) -> dict[str, str]:
    """可选的整理 Agent：值为启动命令或提供它的节点名。

    服务器上的 Hub 本身通常不装 Agent，只看 Hub 自己会漏掉计算节点上的 dsh / codex，
    因此合并在线节点声明的 ``agent:<名称>`` 能力；本地应用另外保留本机检测结果。
    """
    from ..agents import detect_agents, preset_names

    found: dict[str, str] = {} if server_mode(manager) else dict(detect_agents())
    providers: dict[str, list[str]] = {}
    for node, capabilities in _node_capabilities(manager, user):
        for capability in sorted(capabilities):
            name = capability.removeprefix("agent:")
            # agent:custom 是 Hub 所在电脑上的自定义命令，不是可选预设
            if capability.startswith("agent:") and name != "custom":
                providers.setdefault(name, []).append(node)
    # 内置预设排在前面，节点上的插件预设随后，顺序稳定便于网页展示
    order = [name for name in preset_names() if name in providers] + sorted(set(providers) - set(preset_names()))
    for name in order:
        found.setdefault(name, "节点：" + "、".join(providers[name]))
    return found


def config_payload(manager: JobManager, user: User = LOCAL_USER) -> dict:
    from ..asr.audio import SAMPLE_RATE  # noqa: F401  确认依赖可用

    asr_env, llm_env = AsrConfig.from_env(), LlmConfig.from_env()
    from ..agents import agent_model_options, preset_names
    from ..agents.registry import AUTO_PRESET
    from ..frame_selectors.registry import available as available_selectors

    agents = available_agents(manager, user)
    agent_default = default_agent().preset
    if not agent_default and not env("COURSE2MD_AGENT") and AUTO_PRESET in agents:
        # 网页默认选内置模型会让没读图能力的节点接走整理；有节点能跑 Agent 时默认选它
        agent_default = AUTO_PRESET
    has_llm_key = bool(llm_env.resolved_key())
    if server_mode(manager):
        # 服务器自己的密钥不算数：模型接口由具备 llm-api 的节点承担
        has_llm_key = any("llm-api" in caps for _, caps in _node_capabilities(manager, user))

    return {
        "model": asr_env.model,
        "device": asr_env.device,
        "model_path": find_model(asr_env.model),
        "has_llm_key": has_llm_key,
        "llm_base": llm_env.base_url,
        "llm_model": llm_env.model,
        "llm_vision": llm_env.vision,
        "max_images": llm_env.max_images,
        "default_out": str(Path.cwd() / default_out_dir()),
        "term_start": default_term_start(),
        "default_formats": list(default_formats()),
        "proofread": True,
        "detail": default_detail(),
        "frames": default_frames(),
        "frame_interval": default_frame_interval(),
        "frame_selector": default_frame_selector(),
        "frame_selectors": [asdict(info) for info in available_selectors()],
        "delete_video": default_delete_video(),
        "startup_warnings": getattr(manager, "warnings", []),
        "formats": ["srt", "vtt", "txt", "md", "json"],
        "devices": ["cuda", "cpu"],
        "agents": agents,
        "agent_default": agent_default,
        "agent_models": agent_model_options(),
        "agent_choices": {
            name: {"provider": choice.provider, "model": choice.model}
            for name, choice in ((name, default_agent(name)) for name in preset_names())
        },
        "session_batch": True,
        "has_local_node": has_local_node(manager),
        "resolvers": [{"name": info.name, "label": info.label, "hint": info.source_hint} for info in available()],
    }


def _frame_selector(manager: JobManager, requested: object) -> str:
    """校验网页选择的画面筛选策略；留空使用配置中的默认值。"""
    from ..frame_selectors.registry import NAME_PATTERN, available

    if requested is None or requested == "":
        return default_frame_selector()
    if not isinstance(requested, str) or not NAME_PATTERN.fullmatch(requested):
        raise ValueError("画面筛选策略名称无效")
    # 服务器上策略插件可以只装在计算节点，由 frame-selector:<名称> 能力派发；本地应用要求本机已安装
    if not server_mode(manager) and requested not in {info.name for info in available()}:
        names = "、".join(info.name for info in available())
        raise ValueError(f"未安装画面筛选策略「{requested}」，可选 {names}")
    return requested


SESSIONS_WAIT_SECONDS = 90.0


def list_sessions(manager: JobManager, source: str, resolver, user: User = LOCAL_USER) -> list[SessionInfo]:
    """列出来源的课次。

    本地应用的 Hub 就是用户自己的电脑，直接解析。服务器上的 Hub 可能访问不了课程平台
    （例如校内网），因此交给具备 ``resolver:<名称>`` 能力的计算节点解析，再把结果传回。
    """
    queue = getattr(manager, "queue", None)
    if not server_mode(manager) or queue is None:
        return resolver.list_sessions(source)

    options = JobOptions(source=source, out_dir=manager.data_dir / "resolve" / uuid.uuid4().hex)
    requires = SessionsStage.requires(options)
    if not queue.capable_worker_online(requires, owner_id=user.id):
        needed = "、".join(requires) or "对应解析器"
        raise ResolverError(
            f"没有在线的计算节点能解析该来源（需要 {needed}）。"
            "请在「计算服务」中连接一台能访问该课程平台、并安装了对应解析插件的节点"
        )
    # 来源链接可能带登录凭证：任务载荷中的 source 与课程记录一样加密保存，任务结束后立即清除
    job_id = f"sessions-{uuid.uuid4().hex[:12]}"
    task_id = queue.enqueue(
        job_id,
        SessionsStage.name,
        {"options": options_to_dict(options), "inputs": None, "run_id": ""},
        requires=requires,
        max_attempts=2,
        owner_id=user.id,
    )
    deadline = time.monotonic() + SESSIONS_WAIT_SECONDS
    try:
        while True:
            queue.reap()
            task = queue.get(task_id)
            if task is None:
                raise ResolverError("解析任务记录丢失，请重试")
            if task["status"] == "done":
                result = decode(queue.output(task))
                return list(result.sessions)
            if task["status"] in ("failed", "cancelled"):
                error = rebuild_task_error(task)
                # 节点上未归类的异常也按解析失败展示，而不是 500
                raise error if isinstance(error, Course2mdError) else ResolverError(str(error))
            if time.monotonic() > deadline:
                queue.cancel(task_id)
                raise ResolverError("等待计算节点解析超时，请确认节点在线且能访问该课程平台后重试")
            time.sleep(0.2)
    finally:
        queue.discard_job(job_id)


def _stored_sessions(manager: JobManager, body: dict, source: str, user: User, wanted: list[str]) -> list[SessionInfo]:
    """服务器上提交任务时优先使用解析时保存的课次，避免为同一来源再让节点访问一次平台。

    只有保存的课次覆盖了全部选中课次、且来源一致时才使用，否则返回空列表交由调用方重新解析。
    """
    course = manager.courses.get(body.get("course_id") or "")
    if not wanted or not course or not visible(user, course.get("owner")) or course.get("source") != source:
        return []
    stored = {item["id"]: item for item in course.get("sessions", [])}
    if not all(session_id in stored for session_id in wanted):
        return []
    return [
        SessionInfo(
            session_id=item["id"],
            date=item.get("date") or "",
            title=item.get("title") or "",
            views=int(item.get("views") or 0),
            course=item.get("course") or "",
            meta={str(k): str(v) for k, v in (item.get("meta") or {}).items()},
        )
        for item in stored.values()
    ]


def resolve_source(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:
    from .plugins import ensure_ready

    ensure_ready()
    source = (body.get("source") or "").strip()
    if not source:
        raise ValueError("请先填写链接或路径")
    resolver = get_resolver(source)
    _check_source(manager, user, resolver.name)
    if body.get("course_id") and body["course_id"] in manager.courses:
        get_course(manager, body["course_id"], user)
    sessions = list_sessions(manager, source, resolver, user)
    root = output_root(manager, user, body.get("out_dir"))
    resolved = {**_describe_sessions(sessions, root, body.get("term_start", "")), **_resolver_fields(resolver, source)}
    course = manager.remember_course(source, root, resolved, body.get("course_id", ""), owner=user.id)
    snapshot = manager.course_snapshot(course)
    return {**snapshot, "course_id": course["id"]}


def create_targets(manager: JobManager, body: dict, source: str, root: Path, user: User = LOCAL_USER) -> list[dict]:
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
        _check_source(manager, user, resolver_name)
        sessions = (
            _stored_sessions(manager, body, source, user, wanted) if server_mode(manager) else []
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
        folder = root / safe_name(course, "未分类") / safe_name(_session_folder(session, course), "session")
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
        **_describe_sessions(sessions, root, body.get("term_start", "")),
        **(_resolver_fields(resolver, source) if resolver_name else {"resolver": ""}),
    }
    if not resolved["course"]:
        resolved["course"] = targets[0]["course"] or targets[0]["title"]
    course = manager.remember_course(source, root, resolved, body.get("course_id", ""), owner=user.id)
    for target in targets:
        target["course_id"] = course["id"]
    return targets


def _create_job_locked(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:
    from .plugins import ensure_ready

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
    from ..agents import AgentError

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

    frame_selector = _frame_selector(manager, body.get("frame_selector"))

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
        found = _existing_result(target["folder"])
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
    _request_cancel(manager, job)
    job.log("收到取消请求")
    manager.save_job(job)
    return {"ok": True}


STAGE_ORDER = PIPELINE_STAGES


def job_tasks(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> list[dict]:
    """任务各阶段的执行记录（节点、状态、耗时、生成模型），不含选项与输入。"""
    get_job(manager, job_id, user)
    queue = getattr(manager, "queue", None)
    if queue is None:
        return []
    hidden = ("payload", "cache_key")
    return [{key: value for key, value in task.items() if key not in hidden} for task in queue.tasks_for_job(job_id)]


def list_workers(manager: JobManager, user: User = LOCAL_USER) -> list[dict]:
    """共享节点（无归属）对所有人可见，个人节点只对主人可见，管理员看到全部。"""
    queue = getattr(manager, "queue", None)
    workers = queue.workers() if queue is not None else []
    return [item for item in workers if user.is_admin or item["owner_id"] in (None, user.id)]


def rerun_job(manager: JobManager, job_id: str, body: dict, user: User = LOCAL_USER) -> dict:
    """从指定阶段重新运行，之前阶段在输入与设置不变时复用已有结果。可换整理后端与模型。"""
    from .plugins import ensure_ready

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
            from ..agents import AgentError

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


def _require_queue(manager: JobManager):
    queue = getattr(manager, "queue", None)
    if queue is None:
        raise ValueError("Hub 未启用任务队列")
    return queue


def create_worker_token(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:
    """管理员默认创建共享节点（可领取所有人的任务），普通用户创建只处理自己任务的个人节点。"""
    name = str(body.get("name") or "").strip()[:100] or "计算节点"
    shared = user.is_admin and not body.get("personal")
    owner = None if shared else user.id
    worker_id, token = _require_queue(manager).create_worker_token(name, owner_id=owner)
    return {"worker_id": worker_id, "name": name, "token": token, "shared": shared}


def disable_worker(manager: JobManager, worker_id: str, user: User = LOCAL_USER) -> dict:
    queue = _require_queue(manager)
    owner = next((item["owner_id"] for item in queue.workers() if item["id"] == worker_id), "missing")
    if owner == "missing" or not (user.is_admin or owner == user.id):
        raise NotFound("节点不存在或不能停用")
    if not queue.disable_worker(worker_id):
        raise NotFound("节点不存在或不能停用")
    return {"ok": True}


def delete_worker(manager: JobManager, worker_id: str, user: User = LOCAL_USER) -> dict:
    queue = _require_queue(manager)
    owner = next((item["owner_id"] for item in queue.workers() if item["id"] == worker_id), "missing")
    if owner == "missing" or not (user.is_admin or owner == user.id):
        raise NotFound("节点不存在或不能删除")
    if not queue.delete_worker(worker_id):
        raise NotFound("节点不存在或不能删除")
    return {"ok": True}


def _versions(manager: JobManager):
    versions = getattr(manager, "versions", None)
    if versions is None:
        raise NotFound("没有版本记录")
    return versions


def note_versions(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> list[dict]:
    get_job(manager, job_id, user)
    versions = getattr(manager, "versions", None)
    return versions.list(job_id) if versions is not None else []


def note_version_text(manager: JobManager, job_id: str, version: int, kind: str, user: User = LOCAL_USER) -> dict:
    get_job(manager, job_id, user)
    if kind not in ("detailed", "brief"):
        raise ValueError("kind 可选 detailed、brief")
    text = _versions(manager).text(job_id, version, kind)
    if text is None:
        raise NotFound("版本不存在")
    return {"version": version, "kind": kind, "text": text}


def restore_note_version(manager: JobManager, job_id: str, version: int, user: User = LOCAL_USER) -> dict:
    """把两版笔记恢复为指定版本（作为新版本保存），配图沿用当前文件。"""
    job = get_job(manager, job_id, user)
    if job.status in ACTIVE_STATUSES or job.pending_messages:
        raise ValueError("课程正在处理，请稍后再恢复版本")
    if not job.result or not job.result.detailed_notes:
        raise ValueError("这个任务没有笔记文件")
    versions = _versions(manager)
    detailed = versions.text(job_id, version, "detailed")
    if detailed is None:
        raise NotFound("版本不存在")
    job.result.detailed_notes.write_text(detailed, encoding="utf-8", newline="")  # 按原样写回
    brief = versions.text(job_id, version, "brief")
    if brief is not None and job.result.brief_notes:
        job.result.brief_notes.write_text(brief, encoding="utf-8", newline="")
    with job.locked():
        job.revision += 1
        manager.record_version(job, f"恢复到第 {version} 版")
        manager.save_job(job)
    return {"job": job.snapshot(0)}


def _require_hub_links(manager: JobManager, user: User):
    if not user.is_admin:
        raise Forbidden("只有管理员可以管理对外连接的 Hub")
    links = getattr(manager, "hub_links", None)
    if links is None:
        raise NotFound("当前实例未启用对外节点连接")
    return links


def list_hub_links(manager: JobManager, user: User = LOCAL_USER) -> dict[str, Any]:
    links = getattr(manager, "hub_links", None)
    if not user.is_admin or links is None:
        return {
            "links": [],
            "can_manage": False,
            "capabilities": [],
            "default_name": "",
            "default_work_dir": "",
        }
    return {
        "links": links.list_links(),
        "can_manage": True,
        "capabilities": links.local_capabilities(),
        "default_name": platform.node() or "计算节点",
        "default_work_dir": str(links.default_work_dir),
    }


def create_hub_link(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    without = body.get("without")
    if isinstance(without, str):
        without = [item.strip() for item in without.split(",") if item.strip()]
    link = links.add_link(
        str(body.get("hub_url", "")),
        str(body.get("token", "")),
        name=str(body.get("name", "")),
        work_dir=str(body.get("work_dir", "")),
        auto_connect=bool(body.get("auto_connect", True)),
        no_agents=bool(body.get("no_agents", False)),
        without=list(without) if isinstance(without, (list, tuple)) else None,
        connect_now=bool(body.get("connect_now", True)),
        allow_custom_work_dir=not server_mode(manager),
    )
    return {"link": link}


def update_hub_link(manager: JobManager, link_id: str, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    without = body.get("without") if "without" in body else None
    if isinstance(without, str):
        without = [item.strip() for item in without.split(",") if item.strip()]
    try:
        link = links.update_link(
            link_id,
            hub_url=str(body["hub_url"]) if "hub_url" in body and body["hub_url"] is not None else None,
            token=str(body["token"]) if "token" in body and body["token"] is not None else None,
            name=str(body["name"]) if "name" in body and body["name"] is not None else None,
            work_dir=str(body["work_dir"]) if "work_dir" in body and body["work_dir"] is not None else None,
            auto_connect=bool(body["auto_connect"])
            if "auto_connect" in body and body["auto_connect"] is not None
            else None,
            no_agents=bool(body["no_agents"]) if "no_agents" in body and body["no_agents"] is not None else None,
            without=list(without) if isinstance(without, (list, tuple)) else None,
            allow_custom_work_dir=not server_mode(manager),
        )
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc
    return {"link": link}


def connect_hub_link(manager: JobManager, link_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    try:
        return {"link": links.connect(link_id)}
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc


def disconnect_hub_link(manager: JobManager, link_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    try:
        return {"link": links.disconnect(link_id)}
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc


def delete_hub_link(manager: JobManager, link_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    try:
        links.delete_link(link_id)
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc
    return {"ok": True}


def _require_local_nodes(manager: JobManager, user: User):
    if not user.is_admin:
        raise Forbidden("只有管理员可以管理本机节点")
    nodes = getattr(manager, "local_nodes", None)
    if nodes is None:
        raise NotFound("当前实例没有本机节点（纯 Hub 模式），请以 course2md server --with-node 启动")
    return nodes


def list_local_nodes(manager: JobManager, user: User = LOCAL_USER) -> dict[str, Any]:
    nodes = getattr(manager, "local_nodes", None)
    if not user.is_admin or nodes is None:
        return {"nodes": [], "can_manage": False, "available_capabilities": []}
    return {"nodes": nodes.list_nodes(), "can_manage": True, "available_capabilities": nodes.available_capabilities()}


def _capability_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    raise ValueError("capabilities 必须是能力名称列表")


def create_local_node(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    nodes = _require_local_nodes(manager, user)
    return {
        "node": nodes.add_node(
            str(body.get("name") or ""),
            auto=bool(body.get("auto", False)),
            capabilities=_capability_list(body.get("capabilities")),
            enabled=bool(body.get("enabled", True)),
        )
    }


def update_local_node(
    manager: JobManager, node_id: str, body: dict[str, Any], user: User = LOCAL_USER
) -> dict[str, Any]:
    nodes = _require_local_nodes(manager, user)
    try:
        node = nodes.update_node(
            node_id,
            name=str(body["name"]) if body.get("name") is not None else None,
            auto=bool(body["auto"]) if body.get("auto") is not None else None,
            capabilities=_capability_list(body.get("capabilities")),
            enabled=bool(body["enabled"]) if body.get("enabled") is not None else None,
        )
    except KeyError as exc:
        raise NotFound("本机节点不存在") from exc
    return {"node": node}


def delete_local_node(manager: JobManager, node_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    nodes = _require_local_nodes(manager, user)
    queue = _require_queue(manager)
    try:
        nodes.delete_node(node_id, lambda worker_id: queue.delete_worker(worker_id, allow_builtin=True))
    except KeyError as exc:
        raise NotFound("本机节点不存在") from exc
    return {"ok": True}


# ------------------------------------------------------------ 课程产物打包与 Hub 间互传


def _peer_store(manager: JobManager):
    from .transfer import TransferPeerStore

    return manager.shared(
        "transfer-peers",
        lambda: TransferPeerStore(manager.data_dir / "hub-peers.json", cipher=manager.store.cipher),
    )


def _local_origins(manager: JobManager) -> set[str]:
    links = getattr(manager, "hub_links", None)
    return links.local_origin_urls() if links is not None else set()


def _resolve_remote_target(manager: JobManager, body: dict[str, Any], user: User) -> tuple[str, str, str]:
    from .transfer import TransferError, validate_remote_hub_url

    peer_id = str(body.get("peer_id") or "").strip()
    raw_url = str(body.get("hub_url") or "").strip()
    raw_token = str(body.get("token") or "").strip()
    peer_name = ""

    if peer_id:
        peer = _peer_store(manager).get_for(user.id, peer_id)
        if peer is None:
            raise NotFound("对端 Hub 配置不存在")
        raw_url = raw_url or peer.hub_url
        raw_token = raw_token or peer.token
        peer_name = peer.name or peer.hub_url

    if not raw_url:
        raise TransferError("请先填写或选择一个对端 Hub 地址", status_code=400)

    hub_url = validate_remote_hub_url(
        raw_url,
        server_mode=server_mode(manager),
        is_admin=user.is_admin,
        local_origins=_local_origins(manager),
    )
    return hub_url, raw_token, peer_name or hub_url


def export_job_bundle_file(
    manager: JobManager, job_id: str, user: User = LOCAL_USER, *, include_cache: bool = False
) -> tuple[Path, str]:
    from ..jobs.bundle import export_bundle

    job = get_job(manager, job_id, user)
    course = manager.courses.get(job.course_id) if job.course_id else None
    if course is not None and not visible(user, course.get("owner")):
        course = None

    fd, tmp_name = tempfile.mkstemp(prefix="c2m-job-bundle-", suffix=".c2m.zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        export_bundle(manager, [job], tmp_path, course=course, include_cache=include_cache)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    stem = safe_name(f"{job.course}-{job.title}" if job.course else job.title, "course")
    return tmp_path, f"{stem}.c2m.zip"


def export_course_bundle_file(
    manager: JobManager,
    course_id: str,
    job_ids: list[str] | None = None,
    user: User = LOCAL_USER,
    *,
    include_cache: bool = False,
) -> tuple[Path, str]:
    from ..jobs.bundle import export_bundle

    course = get_course(manager, course_id, user)
    with manager.locked():
        owned_jobs = [
            job
            for job in manager.jobs.values()
            if job.course_id == course["id"] and visible(user, job.owner) and job.deleted_at is None
        ]
    if job_ids:
        wanted = [str(item).strip() for item in job_ids if str(item).strip()]
        by_id = {job.id: job for job in owned_jobs}
        missing = [jid for jid in wanted if jid not in by_id]
        if missing:
            raise NotFound("所选课次不存在或不属于该课程")
        selected = [by_id[jid] for jid in wanted]
    else:
        selected = [
            job
            for job in owned_jobs
            if job.status not in ACTIVE_STATUSES and not job.pending_messages and job.out_dir.is_dir()
        ]
    if not selected:
        raise ValueError("该课程目前没有可导出或传输的已完成课次")
    selected.sort(key=lambda item: item.started_at)

    fd, tmp_name = tempfile.mkstemp(prefix="c2m-course-bundle-", suffix=".c2m.zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        export_bundle(manager, selected, tmp_path, course=course, include_cache=include_cache)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    stem = safe_name(str(course.get("title") or course.get("course") or "course"), "course")
    return tmp_path, f"{stem}.c2m.zip"


def import_bundle_file(
    manager: JobManager,
    bundle_path: Path,
    *,
    user: User = LOCAL_USER,
    conflict: str = "skip",
    out_dir: str | None = None,
    origin_label: str = "产物包",
) -> dict[str, Any]:
    from ..jobs.bundle import import_bundle

    root = output_root(manager, user, out_dir)
    return import_bundle(
        manager,
        bundle_path,
        owner=user.id,
        root=root,
        conflict=conflict,
        server_mode=server_mode(manager),
        is_admin=user.is_admin,
        origin_label=origin_label,
    )


def list_transfer_peers(manager: JobManager, user: User = LOCAL_USER) -> dict[str, Any]:
    return {"peers": _peer_store(manager).list_for(user.id)}


def create_transfer_peer(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from .transfer import validate_remote_hub_url

    hub_url = validate_remote_hub_url(
        str(body.get("hub_url") or ""),
        server_mode=server_mode(manager),
        is_admin=user.is_admin,
        local_origins=_local_origins(manager),
    )
    peer = _peer_store(manager).add(
        user.id,
        hub_url,
        token=str(body.get("token") or ""),
        name=str(body.get("name") or ""),
    )
    return {"peer": peer}


def update_transfer_peer(
    manager: JobManager, peer_id: str, body: dict[str, Any], user: User = LOCAL_USER
) -> dict[str, Any]:
    from .transfer import validate_remote_hub_url

    hub_url = None
    if "hub_url" in body and body["hub_url"] is not None:
        hub_url = validate_remote_hub_url(
            str(body["hub_url"]),
            server_mode=server_mode(manager),
            is_admin=user.is_admin,
            local_origins=_local_origins(manager),
        )
    token = str(body["token"]) if "token" in body and body["token"] is not None else None
    name = str(body["name"]) if "name" in body and body["name"] is not None else None
    try:
        peer = _peer_store(manager).update(user.id, peer_id, hub_url=hub_url, token=token, name=name)
    except KeyError as exc:
        raise NotFound("对端 Hub 配置不存在") from exc
    return {"peer": peer}


def delete_transfer_peer(manager: JobManager, peer_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    try:
        _peer_store(manager).delete(user.id, peer_id)
    except KeyError as exc:
        raise NotFound("对端 Hub 配置不存在") from exc
    return {"ok": True}


def remote_catalog(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from .transfer import RemoteHubClient

    hub_url, token, _ = _resolve_remote_target(manager, body, user)
    with RemoteHubClient(hub_url, token) as remote:
        return remote.catalog()


def transfer_push(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from ..jobs.bundle import CONFLICT_MODES, export_bundle
    from .transfer import RemoteHubClient

    hub_url, token, _ = _resolve_remote_target(manager, body, user)
    conflict = str(body.get("conflict") or "skip")
    if conflict not in CONFLICT_MODES:
        raise ValueError(f"不支持的冲突处理策略：{conflict!r}，可选 {', '.join(CONFLICT_MODES)}")
    include_cache = bool(body.get("include_cache", False))

    raw_job_ids = body.get("job_ids")
    course_id = str(body.get("course_id") or "").strip()

    groups: list[tuple[dict[str, Any] | None, list[Job]]] = []
    if isinstance(raw_job_ids, list) and raw_job_ids:
        by_course: dict[str, list[Job]] = {}
        for jid in dict.fromkeys(str(item).strip() for item in raw_job_ids if str(item).strip()):
            job = get_job(manager, jid, user)
            if course_id and job.course_id != course_id:
                raise ValueError(f"课次「{job.title}」不属于指定课程")
            by_course.setdefault(job.course_id or f"standalone:{job.id}", []).append(job)
        for key, jobs in by_course.items():
            course = manager.courses.get(key) if not key.startswith("standalone:") else None
            if course is not None and not visible(user, course.get("owner")):
                course = None
            groups.append((course, jobs))
    elif course_id:
        course = get_course(manager, course_id, user)
        with manager.locked():
            jobs = [
                job
                for job in manager.jobs.values()
                if job.course_id == course["id"]
                and visible(user, job.owner)
                and job.deleted_at is None
                and job.status not in ACTIVE_STATUSES
                and not job.pending_messages
                and job.out_dir.is_dir()
            ]
        if not jobs:
            raise ValueError("该课程没有可推送的已完成课次")
        jobs.sort(key=lambda item: item.started_at)
        groups.append((course, jobs))
    else:
        raise ValueError("请先选择要推送的课程或课次")

    courses_by_id: dict[str, dict[str, Any]] = {}
    imported: list[dict[str, Any]] = []
    overwritten: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    with RemoteHubClient(hub_url, token) as remote:
        for course, jobs in groups:
            fd, tmp_name = tempfile.mkstemp(prefix="c2m-push-", suffix=".c2m.zip")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                export_bundle(manager, jobs, tmp_path, course=course, include_cache=include_cache)
                result = remote.push_bundle(tmp_path, conflict=conflict)
            finally:
                tmp_path.unlink(missing_ok=True)
            remote_course = result.get("course")
            if isinstance(remote_course, dict) and remote_course.get("id"):
                courses_by_id[str(remote_course["id"])] = remote_course
            imported.extend(result.get("imported") or [])
            overwritten.extend(result.get("overwritten") or [])
            skipped.extend(result.get("skipped") or [])
            warnings.extend(result.get("warnings") or [])

    return {
        "direction": "push",
        "hub_url": hub_url,
        "courses": list(courses_by_id.values()),
        "imported": imported,
        "overwritten": overwritten,
        "skipped": skipped,
        "warnings": warnings,
    }


def transfer_pull(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from ..jobs.bundle import CONFLICT_MODES
    from .transfer import RemoteHubClient

    hub_url, token, peer_name = _resolve_remote_target(manager, body, user)
    conflict = str(body.get("conflict") or "skip")
    if conflict not in CONFLICT_MODES:
        raise ValueError(f"不支持的冲突处理策略：{conflict!r}，可选 {', '.join(CONFLICT_MODES)}")

    include_cache = bool(body.get("include_cache", False))
    course_id = str(body.get("course_id") or "").strip()
    raw_job_ids = body.get("job_ids")
    job_ids = (
        list(dict.fromkeys(str(item).strip() for item in raw_job_ids if str(item).strip()))
        if isinstance(raw_job_ids, list)
        else []
    )
    if not course_id and not job_ids:
        raise ValueError("请先选择要拉取的课程或课次")

    out_dir = str(body.get("out_dir") or "").strip() or None
    courses_by_id: dict[str, dict[str, Any]] = {}
    imported: list[dict[str, Any]] = []
    overwritten: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    def _absorb(result: dict[str, Any]) -> None:
        item_course = result.get("course")
        if isinstance(item_course, dict) and item_course.get("id"):
            courses_by_id[str(item_course["id"])] = item_course
        imported.extend(result.get("imported") or [])
        overwritten.extend(result.get("overwritten") or [])
        skipped.extend(result.get("skipped") or [])
        warnings.extend(result.get("warnings") or [])

    with RemoteHubClient(hub_url, token) as remote:
        if course_id:
            fd, tmp_name = tempfile.mkstemp(prefix="c2m-pull-", suffix=".c2m.zip")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                remote.download_course_bundle(course_id, tmp_path, job_ids=job_ids or None, include_cache=include_cache)
                _absorb(
                    import_bundle_file(
                        manager,
                        tmp_path,
                        user=user,
                        conflict=conflict,
                        out_dir=out_dir,
                        origin_label=f"对端 Hub（{peer_name}）",
                    )
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            for jid in job_ids:
                fd, tmp_name = tempfile.mkstemp(prefix="c2m-pull-", suffix=".c2m.zip")
                os.close(fd)
                tmp_path = Path(tmp_name)
                try:
                    remote.download_job_bundle(jid, tmp_path, include_cache=include_cache)
                    _absorb(
                        import_bundle_file(
                            manager,
                            tmp_path,
                            user=user,
                            conflict=conflict,
                            out_dir=out_dir,
                            origin_label=f"对端 Hub（{peer_name}）",
                        )
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)

    return {
        "direction": "pull",
        "hub_url": hub_url,
        "courses": list(courses_by_id.values()),
        "imported": imported,
        "overwritten": overwritten,
        "skipped": skipped,
        "warnings": warnings,
    }

"""来源解析：列出课次、按来源解析与刷新课程。"""

from __future__ import annotations

import time
import uuid

from ...core.accounts import LOCAL_USER, User
from ...errors import Course2mdError
from ...jobs.manager import JobManager
from ...jobs.sessions import describe_sessions, resolver_fields
from ...resolvers import (
    ResolverError,
    get_resolver,
)
from ...resolvers.base import SessionInfo
from ...stages.codec import decode, options_to_dict
from ...stages.executor import rebuild_task_error
from ...stages.options import JobOptions
from ...stages.sessions import SessionsStage
from ..access import Forbidden, server_mode
from ..plugins import ensure_ready
from .common import get_course, has_local_node, output_root, visible


def check_source(manager: JobManager, user: User, resolver_name: str) -> None:
    if resolver_name != "local":
        return
    if server_mode(manager) and not user.is_admin:
        raise Forbidden("服务器上不能读取本地文件，请使用课程平台链接，或在自己的电脑上运行本地应用")
    if server_mode(manager) and not has_local_node(manager):
        raise ResolverError(
            "当前服务器以纯 Hub 模式运行（未启用本机节点），不能读取服务器本地文件；"
            "请使用课程平台链接，或以 course2md server --with-node 启动"
        )


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


def stored_sessions(manager: JobManager, body: dict, source: str, user: User, wanted: list[str]) -> list[SessionInfo]:
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
    ensure_ready()
    source = (body.get("source") or "").strip()
    if not source:
        raise ValueError("请先填写链接或路径")
    resolver = get_resolver(source)
    check_source(manager, user, resolver.name)
    if body.get("course_id") and body["course_id"] in manager.courses:
        get_course(manager, body["course_id"], user)
    sessions = list_sessions(manager, source, resolver, user)
    root = output_root(manager, user, body.get("out_dir"))
    resolved = {**describe_sessions(sessions, root, body.get("term_start", "")), **resolver_fields(resolver, source)}
    course = manager.remember_course(source, root, resolved, body.get("course_id", ""), owner=user.id)
    snapshot = manager.course_snapshot(course)
    return {**snapshot, "course_id": course["id"]}


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

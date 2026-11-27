"""课程与任务记录、按课次并行编排、课程对话。GPU 并发由节点的串行执行器限制。"""

from __future__ import annotations

import hashlib
import json
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from markdown_it import MarkdownIt

from ..config import (
    default_out_dir,
    default_term_start,
    default_worker_dir,
)
from ..core.queue import TaskQueue
from ..core.versions import NoteVersions
from ..pipeline import JobResult, run_job
from ..resolvers import (
    course_identity,
    parse_date,
    resolve_term_start,
    view_options,
    week_of,
)
from ..stages.codec import options_from_dict, options_to_dict
from ..stages.executor import QueueExecutor, Suspended
from ..stages.options import JobOptions
from ..storage import LocalFileStore
from ..util.text import safe_name
from .records import CourseRecord, CourseView, ExistingResult, MessageRecord, ResolvedCourse, SessionRecord, SessionView
from .workspace_store import WorkspaceStore

if TYPE_CHECKING:
    from ..worker import LocalNodeManager, WorkerRunner

ACTIVE_STATUSES = ("queued", "running")
TRASH_SECONDS = 30 * 24 * 3600  # 回收站保留期
PURGE_INTERVAL = 3600.0
FORMATS = ("srt", "vtt", "txt", "md", "json")


def _markdown_html(text: str) -> str:
    return MarkdownIt("commonmark", {"html": False}).disable("image").render(text)


T = TypeVar("T")


@dataclass
class Job:
    """课程任务及后续对话，输出游标在缓冲区裁剪后继续单调递增。"""

    id: str
    source: str
    title: str
    out_dir: Path
    course: str = ""
    course_id: str = ""
    session_id: str = ""
    session_date: str = ""
    week: int | None = None
    status: str = "queued"
    stage: str = "排队"
    progress: float = 0.0
    sub: float | None = None
    message: str = "等待执行"
    logs: list[str] = field(default_factory=list)
    files: list[dict[str, str]] = field(default_factory=list)
    error: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    started_at: float = field(default_factory=time.time)
    _log_bucket: tuple[str, int] = ("", -1)
    agent: str = ""
    agent_model: str = ""
    owner: str = "local"  # 所属用户：本地模式为 local，服务器形态为用户编号
    deleted_at: float | None = None  # 移入回收站的时间；超过保留期后永久删除
    timings: dict[str, float] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    messages: list[MessageRecord] = field(default_factory=list)
    pending_messages: int = 0
    revision: int = 0
    options: JobOptions | None = field(default=None, repr=False)
    result: JobResult | None = field(default=None, repr=False)
    reuse: frozenset = field(default_factory=frozenset, repr=False)  # 下次运行复用之前结果的阶段
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    active_operation: str = "pipeline"
    _event_seq: int = 0
    _log_count: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _html_cache: dict = field(default_factory=dict, repr=False)

    def _message_html(self, message: MessageRecord) -> str:
        """助手消息按内容缓存渲染结果，轮询时不重复解析 Markdown。"""
        if message["role"] != "assistant":
            return ""
        key = (message.get("id"), message["text"])
        if key not in self._html_cache:
            self._html_cache[key] = _markdown_html(message["text"])
        return self._html_cache[key]

    def locked(self) -> AbstractContextManager[bool]:
        """持有任务状态的锁；跨模块修改任务字段必须走它，不要直接访问 ``_lock``。"""
        return self._lock

    def log(self, text: str) -> None:
        with self._lock:
            self.logs.append(f"[{time.strftime('%H:%M:%S')}] {text}")
            self._log_count += 1
            del self.logs[:-500]

    def emit(self, event: dict) -> None:
        with self._lock:
            self._event_seq += 1
            text = str(event.get("text", ""))
            self.events.append(
                {
                    **event,
                    "text": text[:65536],
                    "truncated": len(text) > 65536 or event.get("truncated", False),
                    "seq": self._event_seq,
                    "time": time.time(),
                }
            )
            del self.events[:-400]

    def snapshot(self, since: int = 0, after: int = 0) -> dict:
        with self._lock:
            log_start = self._log_count - len(self.logs)
            return {
                "id": self.id,
                "source": self.source,
                "title": self.title,
                "out_dir": str(self.out_dir),
                "status": self.status,
                "stage": self.stage,
                "course": self.course,
                "session_date": self.session_date,
                "week": self.week,
                "course_id": self.course_id,
                "session_id": self.session_id,
                "progress": round(self.progress, 4),
                "sub": self.sub,
                "message": self.message,
                "logs": self.logs[max(0, since - log_start) :],
                "log_offset": self._log_count,
                "files": list(self.files),
                "error": self.error,
                "agent": self.agent,
                "agent_model": self.agent_model,
                "timings": dict(self.timings),
                "started_at": self.started_at,
                "events": [dict(event) for event in self.events if event["seq"] > after],
                "event_offset": self._event_seq,
                "messages": [{**message, "html": self._message_html(message)} for message in self.messages],
                "pending_messages": self.pending_messages,
                "revision": self.revision,
                "can_chat": bool(
                    self.options
                    and self.options.ai
                    and ((self.result and self.result.note_workspace) or self.status in ("queued", "running"))
                ),
            }


@dataclass
class ChatRequest:
    """排入同一课程队列的用户请求。"""

    id: str
    text: str


class ConfirmationRequired(Exception):
    """需要用户二次确认（例如覆盖已有结果）才能继续提交。"""

    def __init__(self, payload: dict) -> None:
        super().__init__(payload.get("error") or "需要确认")
        self.payload = payload


_TITLE_DATE = re.compile(r"[ _\-]?(\d{4})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?")
_TIME_PART = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})")


def _session_folder(session, course: str = "") -> str:
    """课次在课程文件夹下的名字：优先「日期 时-分」，其次标题。"""
    begin = session.meta.get("begin") or session.date or ""
    found = _TIME_PART.search(begin)
    if found:
        return f"{found[1]} {found[2]}-{found[3]}"
    if session.date:
        return session.date
    title = session.title
    if course and title.startswith(course):
        title = title[len(course) :].strip()
    return safe_name(title or session.session_id, session.session_id or "session")


def _existing_result(folder: Path) -> ExistingResult | None:
    """检查课次目录里是否已有上一次的结果（完整或中断）。"""
    try:
        if not folder.is_dir():
            return None
        entries = list(folder.iterdir())
    except OSError:
        return None
    if not any(entry.is_file() for entry in entries):
        return None
    notes: list[str] = []
    updated = 0.0
    note_dir = folder / "notes"
    note_entries = [*entries, *note_dir.glob("*.md")]
    for path in note_entries:
        try:
            updated = max(updated, path.stat().st_mtime)
        except OSError:
            continue
        if path.is_file() and path.name.endswith((".notes.md", ".brief.md")):
            notes.append(path.relative_to(folder).as_posix())
    complete = False
    manifest = folder / "job.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        detailed = data.get("detailed_notes")
        complete = bool(notes) and bool(detailed) and Path(detailed).is_file()
    return {
        "path": str(folder),
        "complete": complete,
        "file_count": len(entries),
        "updated_at": round(updated, 3),
        "notes": sorted(notes),
    }


def _resolver_fields(resolver, source: str) -> dict[str, Any]:
    """解析器提供给界面的信息：名称、显示名与可选机位。"""
    return {
        "resolver": resolver.name,
        "resolver_label": getattr(resolver, "label", "") or resolver.name,
        "view_options": [
            {"id": option.id, "label": option.label, "default": option.default}
            for option in view_options(resolver, source)
        ],
    }


def _describe_sessions(sessions, root: Path, configured: str = "") -> ResolvedCourse:
    hint = next((session.meta.get("term_start") for session in sessions if session.meta.get("term_start")), "")
    term_start, basis = resolve_term_start(
        configured or default_term_start(), hint, [session.date for session in sessions]
    )
    payload: list[SessionRecord] = []
    for session in sessions:
        day = parse_date(session.date)
        week = week_of(day, term_start) if day and term_start else None
        folder = (
            root / safe_name(session.course, "未分类") / safe_name(_session_folder(session, session.course), "session")
        )
        payload.append(
            {
                "id": session.session_id,
                "date": session.date,
                "title": session.title,
                "views": session.views,
                "week": week,
                "course": session.course,
                "meta": session.meta,
                "folder": str(folder),
            }
        )
    return {
        "course": sessions[0].course if sessions else "",
        "sessions": payload,
        "term_start": term_start.isoformat() if term_start else "",
        "term_source": basis,
    }


class JobManager:
    """不同课次并行编排，同一课次的提取与对话保持顺序。"""

    def __init__(
        self,
        store_path: Path | None = None,
        builtin_worker: bool = True,
        database_url: str = "",
        mode: str = "local",
    ) -> None:
        self.jobs: dict[str, Job] = {}
        self.courses: dict[str, CourseRecord] = {}
        self.warnings: list[str] = []
        # 服务器形态下为 server，按用户隔离并限制可用操作；内置节点的能力也随形态不同，所以要在构造时给定
        self.mode = mode
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._pending: dict[str, list] = {}
        self._shared: dict[str, Any] = {}
        self._runners: dict[str, threading.Thread] = {}
        data_dir = store_path.parent if store_path else Path(default_out_dir()).expanduser().resolve() / ".course2md"
        if database_url:
            from ..core.db import Database

            self.store = WorkspaceStore(data_dir / "workspace.sqlite3", db=Database(database_url))
        else:
            self.store = WorkspaceStore(store_path or data_dir / "workspace.sqlite3")
        self.data_dir = data_dir
        self.courses = {course["id"]: course for course in self.store.load_courses()}
        for data in self.store.load_jobs():
            try:
                job = self._restore_job(data)
                self.jobs[job.id] = job
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.warnings.append(f"任务 {data.get('id', '')} 读取失败（{exc}）")
        self.queue = TaskQueue(self.store.db, LocalFileStore(data_dir / "objects"))
        self.versions = NoteVersions(self.store.db, self.queue.store)
        from ..worker import HubLinkManager, LocalNodeManager, detect_capabilities
        from ..worker.local import BUILTIN_ID

        self._worker_resources: dict = {}
        self._worker_exec_lock = threading.Lock()
        self.local_nodes: LocalNodeManager | None = None
        if builtin_worker:
            self.local_nodes = LocalNodeManager(
                data_dir / "local-nodes.json",
                self.queue,
                resources=self._worker_resources,
                exec_lock=self._worker_exec_lock,
                # 服务器形态的 Hub 不承担模型接口调用，校对与整理交给自带密钥或 Agent 的计算节点
                ceiling=lambda: detect_capabilities(hub=True, llm_api=mode != "server"),
            ).start()
        self._builtin_id = BUILTIN_ID
        self.hub_links = HubLinkManager(
            data_dir / "hub-links.json",
            resources=self._worker_resources,
            exec_lock=self._worker_exec_lock,
            default_work_dir=default_worker_dir(),
        )
        self._worker = threading.Thread(target=self._dispatch, daemon=True)
        self._worker.start()
        for job in list(self.jobs.values()):
            if job.options:
                if job.status == "queued" and job.active_operation == "pipeline":
                    self._queue.put((job, job.options))
                for message in job.messages:
                    if message["role"] == "user" and message.get("status") == "queued":
                        self._queue.put((job, ChatRequest(message["id"], message["text"])))
        self._janitor = threading.Thread(target=self._purge_loop, daemon=True)
        self._janitor.start()

    def locked(self) -> AbstractContextManager[bool]:
        """持有管理器级的锁，保护 ``jobs`` / ``courses`` 两张表。

        锁顺序：先管理器锁，后任务锁；反过来会死锁。外部模块只通过这个方法获取，不要访问 ``_lock``。
        """
        return self._lock

    def shared(self, key: str, factory: Callable[[], T]) -> T:
        """取得按键缓存的共享对象，首次访问时创建。

        给上层（Hub）挂接自己的存储用：``jobs`` 层不能导入 ``hub``，又需要让同一个实例随管理器存活。
        """
        with self._lock:
            if key not in self._shared:
                self._shared[key] = factory()
            return cast(T, self._shared[key])

    @property
    def builtin_worker(self) -> WorkerRunner | None:
        """内置节点的运行器；节点被停用或本实例没有本机节点时为 None。"""
        if self.local_nodes is None:
            return None
        return self.local_nodes.runner(self._builtin_id)

    # ------------------------------------------------------------ 回收站
    def mark_job_deleted(self, job: Job, when: float | None) -> None:
        """移入（when 为时间戳）或移出（None）回收站。"""
        with job.locked():
            job.deleted_at = when
        self.save_job(job)

    def mark_course_deleted(self, course: CourseRecord, when: float | None) -> None:
        with self._lock:
            if when is None:
                course.pop("deleted_at", None)
            else:
                course["deleted_at"] = when
            self.store.save_course(course)

    def course_jobs(self, course: CourseRecord) -> list[Job]:
        with self._lock:
            return [
                job for job in self.jobs.values() if job.course_id == course["id"] and job.owner == course.get("owner")
            ]

    def purge_job(self, job: Job) -> None:
        """永久删除任务：数据库记录、阶段记录与版本；服务器形态下还删除其输出目录。

        本地应用的输出目录由用户自己选择（可能是知识库），只清理记录，不删除用户磁盘上的文件。
        """
        with self._lock:
            self.jobs.pop(job.id, None)
        self.store.delete_job(job.id)
        self.queue.discard_job(job.id)
        self.versions.discard(job.id)
        if self.mode == "server":
            self._remove_output(job.out_dir)

    def purge_course(self, course: CourseRecord) -> None:
        for job in self.course_jobs(course):
            if job.status in ACTIVE_STATUSES:
                continue
            self.purge_job(job)
        with self._lock:
            if self.course_jobs(course):
                return  # 仍有活动任务，稍后再清理
            self.courses.pop(course["id"], None)
        self.store.delete_course(course["id"])

    def purge_deleted(self, now: float | None = None, retention: float = TRASH_SECONDS) -> int:
        """永久删除超过保留期的回收站内容，返回清理的任务数。"""
        cutoff = (time.time() if now is None else now) - retention
        with self._lock:
            jobs = [
                job
                for job in self.jobs.values()
                if job.deleted_at is not None and job.deleted_at <= cutoff and job.status not in ACTIVE_STATUSES
            ]
            courses = [
                course
                for course in self.courses.values()
                if (deleted_at := course.get("deleted_at")) and deleted_at <= cutoff
            ]
        for job in jobs:
            self.purge_job(job)
        for course in courses:
            self.purge_course(course)
        return len(jobs)

    def _remove_output(self, out_dir: Path) -> None:
        """只删除服务器托管目录（输出根目录下的 users/）内的内容，并清理变空的课程文件夹。"""
        base = (Path(default_out_dir()).expanduser().resolve() / "users").resolve()
        target = Path(out_dir).resolve()
        if target == base or not target.is_relative_to(base):
            return
        shutil.rmtree(target, ignore_errors=True)
        parent = target.parent
        # 只清理变空的课程文件夹，保留 users/<用户编号> 这一层
        while parent.parent != base and parent.is_relative_to(base):
            try:
                parent.rmdir()  # 非空时抛出 OSError，保留
            except OSError:
                break
            parent = parent.parent

    def prune_objects(self, grace_seconds: float = 3600.0, now: float | None = None) -> int:
        """回收对象存储中已无引用且超过宽限期的孤立对象（如已发布的临时上传块、已清除任务的产物）。"""
        return self.queue.prune_unreferenced_objects(grace_seconds=grace_seconds, now=now)

    def _purge_loop(self) -> None:
        while not self._closing.is_set():
            try:
                self.purge_deleted()
                self.prune_objects()
            except Exception as exc:  # noqa: BLE001  清理失败不应影响服务，下个周期重试
                self.warnings.append(f"清理回收站失败（{exc}）")
            self._closing.wait(PURGE_INTERVAL)

    def submit(self, job: Job, options: JobOptions) -> Job:
        if getattr(self, "_closing", threading.Event()).is_set():
            raise ValueError("服务正在退出，请稍后重试")
        job.options = options
        job.run_id = uuid.uuid4().hex
        job.active_operation = "pipeline"
        job.messages.append({"id": uuid.uuid4().hex, "role": "user", "text": f"整理课程 {job.title}", "status": "done"})
        with self._lock:
            self.save_job(job, replace_existing=True)
            self.jobs[job.id] = job
        self._queue.put((job, options))
        return job

    def remember_course(
        self, source: str, root: Path, resolved: Mapping[str, Any], course_id: str = "", owner: str = "local"
    ) -> CourseRecord:
        """保存解析信息。解析器提供的课程标识在更换凭证后保持不变，因此会复用同一课程记录。

        不同用户解析同一来源时得到各自的课程记录。
        """
        identity = course_identity(source)
        key = hashlib.sha256(f"{owner}\n{root}\n{identity}".encode()).hexdigest()[:20]
        with self._lock:
            previous: Mapping[str, Any] = self.courses.get(course_id) or self.courses.get(key) or {}
            if previous.get("owner", owner) != owner:
                previous = {}
            course = cast(
                CourseRecord,
                {
                    **previous,
                    **resolved,
                    "id": previous.get("id") or key,
                    "owner": owner,
                    "source": source,
                    "out_dir": str(root),
                    "title": resolved.get("course") or previous.get("title") or Path(source).stem or "课程",
                    "updated_at": time.time(),
                },
            )
            course.pop("deleted_at", None)  # 重新解析同一来源视为恢复该课程；其下已删除的任务仍留在回收站
            # 没有解析到课次时（例如任务创建时解析失败），保留已经保存的完整课次。
            if not resolved.get("sessions") and previous.get("sessions"):
                course["sessions"] = previous["sessions"]
                course["term_start"] = previous.get("term_start", "")
                course["term_source"] = previous.get("term_source", "")
            self.store.save_course(course)
            self.courses[course["id"]] = course
            return course

    def course_snapshot(self, course: CourseRecord) -> CourseView:
        with self._lock:
            related = [job for job in self.jobs.values() if job.course_id == course["id"] and job.deleted_at is None]
        sessions: list[SessionView] = []
        for session in course.get("sessions", []):
            current = next(
                (job for job in related if job.session_id == session["id"] or str(job.out_dir) == session["folder"]),
                None,
            )
            sessions.append(
                {
                    **session,
                    "existing": _existing_result(Path(session["folder"])),
                    "job_id": current.id if current else "",
                    "status": current.status if current else "",
                }
            )
        return {**course, "sessions": sessions}

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def start_hub_links(self, local_origins: set[str] | None = None) -> list[str]:
        """同步环境变量并启动自动连接的外部 Hub（网状节点）。"""
        if local_origins:
            self.hub_links.set_local_origins(local_origins)
        return self.hub_links.start_auto()

    def close(self) -> None:
        """暂停编排，保留未完成任务供重启后接续。"""
        self._closing.set()
        self._queue.put(None)
        self._worker.join(timeout=15)
        for thread in list(self._runners.values()):
            thread.join(timeout=5)
        hub_links = getattr(self, "hub_links", None)
        if hub_links is not None:
            hub_links.close()
        local_nodes = getattr(self, "local_nodes", None)
        if local_nodes is not None:
            local_nodes.close()
        if self._worker.is_alive():
            print("后台请求尚在退出，已停止课程服务。")
        for job in list(self.jobs.values()):
            self.save_job(job)

    def send(self, job: Job, text: str, request_id: str) -> None:
        with job.locked():
            if any(message["id"] == request_id for message in job.messages):
                return
            if not job.snapshot()["can_chat"]:
                raise ValueError("当前课程缺少可续接的笔记上下文，请先完成整理")
            job.messages.append({"id": request_id, "role": "user", "text": text, "status": "queued"})
            job.pending_messages += 1
            if job.status not in ("queued", "running"):
                job.active_operation = "chat"
                job.status, job.stage, job.message = "queued", "对话", "修改要求已排队"
                job.cancel.clear()
            self.save_job(job)
            self._queue.put((job, ChatRequest(request_id, text)))

    def save_job(self, job: Job, replace_existing: bool = False) -> None:
        with job.locked():
            data = {
                item.name: getattr(job, item.name)
                for item in fields(Job)
                if not item.name.startswith("_") and item.name not in ("cancel", "options", "result", "reuse")
            }
            data["reuse"] = sorted(job.reuse)
            data["cancel_requested"] = job.cancel.is_set()
            data["options"] = options_to_dict(job.options) if job.options else None
            data["result"] = (
                {
                    name: str(getattr(job.result, name)) if getattr(job.result, name) else None
                    for name in ("notes", "detailed_notes", "brief_notes", "note_workspace")
                }
                if job.result
                else None
            )
            data["log_offset"], data["event_offset"] = job._log_count, job._event_seq
            self.store.save_job(data, replace_existing=replace_existing)

    def _restore_job(self, data: dict) -> Job:
        values = {
            item.name: data[item.name]
            for item in fields(Job)
            if item.name in data and not item.name.startswith("_") and item.name not in ("cancel", "options", "result")
        }
        values["out_dir"] = Path(values["out_dir"])
        values["reuse"] = frozenset(values.get("reuse") or ())
        job = Job(**values)
        if data.get("cancel_requested"):
            job.cancel.set()
        if data.get("options"):
            job.options = options_from_dict(data["options"])
        result = data.get("result")
        if result:
            job.result = JobResult(
                output_dir=job.out_dir,
                timings=job.timings,
                **{key: Path(value) if value else None for key, value in result.items()},  # type: ignore[arg-type]
            )
        if job.out_dir.is_dir():
            job.files = _collect_files(job.result or JobResult(output_dir=job.out_dir))
        job._log_count = data["log_offset"]
        job._event_seq = data["event_offset"]
        if job.status in ACTIVE_STATUSES:
            job.status, job.stage, job.message = "queued", "恢复", "服务已重启，正在接续未完成阶段"
            job.sub = None
        job.pending_messages = 0
        for message in job.messages:
            if message.get("status") in ACTIVE_STATUSES:
                message["status"] = "queued"
                if message["role"] == "user":
                    job.pending_messages += 1
        return job

    def _dispatch(self) -> None:
        """不同课程独立编排，同一课程的整理与对话严格串行。"""
        while True:
            item = self._queue.get()
            if item is None:
                return
            job, _payload = item
            with self._lock:
                self._pending.setdefault(job.id, []).append(item)
                if job.id not in self._runners:
                    thread = threading.Thread(target=self._run_course, args=(job.id,), daemon=True)
                    self._runners[job.id] = thread
                    thread.start()

    def _run_course(self, job_id: str) -> None:
        try:
            while not self._closing.is_set():
                with self._lock:
                    pending = self._pending.get(job_id, [])
                    if not pending:
                        self._runners.pop(job_id, None)
                        self._pending.pop(job_id, None)
                        return
                    item = pending.pop(0)
                inbox: queue.Queue = queue.Queue()
                inbox.put(item)
                inbox.put(None)
                self._run(inbox)
        finally:
            with self._lock:
                if self._runners.get(job_id) is threading.current_thread():
                    self._runners.pop(job_id, None)

    def _run(self, inbox=None) -> None:  # noqa: PLR0915  历史遗留的长函数，拆分后移除
        while True:
            item = (inbox or self._queue).get()
            if item is None:
                return
            job, payload = item
            if isinstance(payload, ChatRequest):
                self._chat(job, payload)
                continue
            options = payload
            job.options = options
            if job.cancel.is_set():
                job.status, job.message = "cancelled", "已取消"
                self.save_job(job)
                continue
            job.status = "running"
            job.log("开始执行")
            self.save_job(job)

            def progress(stage, fraction, message, sub=None, job=job):
                with job.locked():
                    job.stage, job.progress, job.sub, job.message = stage, max(0.0, min(1.0, fraction)), sub, message
                    scale = sub if sub is not None else job.progress
                    bucket = (stage, int(scale * 10))
                    if message and bucket != job._log_bucket:
                        job._log_bucket = bucket
                        job.log(f"{stage} · {message}")
                        self.save_job(job)

            try:
                # 阶段经任务队列交给节点执行；内置 Worker 复用已加载的转写模型。
                queue = getattr(self, "queue", None)
                executor = (
                    QueueExecutor(
                        queue,
                        job.id,
                        reuse=job.reuse,
                        owner_id=job.owner or None,
                        run_id=job.run_id,
                        stop_event=getattr(self, "_closing", None),
                    )
                    if queue
                    else None
                )
                result = run_job(options, progress=progress, cancel=job.cancel, on_output=job.emit, executor=executor)
                job.result = result
                job.files = _collect_files(result)
                job.revision += 1
            except Suspended:
                job.status, job.stage, job.message = "queued", "恢复", "等待服务重启后继续"
                self.save_job(job)
                continue
            except Exception as exc:  # noqa: BLE001  产物提取失败由该任务展示，队列继续服务后续课程。
                self._failure(job, exc)
                continue
            job.status, job.progress, job.stage = "done", 1.0, "完成"
            job.timings = result.timings
            job.message = "详细版与缩略版已生成" if result.detailed_notes else "转写完成"
            if "整理" in result.timings:
                job.message += f"，整理耗时 {result.timings['整理']:.1f} 秒"
            if result.deleted_media:
                job.message += f"，已清理 {len(result.deleted_media)} 个下载视频"
            job.messages.append({"id": uuid.uuid4().hex, "role": "assistant", "text": job.message, "status": "done"})
            job.log(job.message)
            for warning in result.warnings:
                job.log(warning)
            self.record_version(job, "整理")
            self.save_job(job)

    def record_version(self, job: Job, label: str) -> None:
        versions = getattr(self, "versions", None)
        if versions is None or not job.result or not job.result.detailed_notes:
            return
        try:
            model = job.agent_model or job.agent
            versions.record(job.id, job.result.detailed_notes, job.result.brief_notes, label, model)
        except OSError as exc:  # 版本快照失败不影响笔记本身
            job.log(f"笔记版本保存失败：{exc}")

    def _failure(self, job: Job, exc: Exception) -> None:
        with job.locked():
            job.status = "cancelled" if job.cancel.is_set() else "failed"
            job.error = "已取消" if job.cancel.is_set() else str(exc) or type(exc).__name__
            job.message = job.error
            job.log(job.error)
            job.log(traceback.format_exc(limit=4))
            self.save_job(job)

    def _chat(self, job: Job, request: ChatRequest) -> None:  # noqa: PLR0915  历史遗留的长函数，拆分后移除
        from ..refine.conversation import continue_notes
        from ..stages.artifacts import ChatInput
        from ..stages.base import StageContext
        from ..stages.chat import ChatStage

        with job.locked():
            job.active_operation = "chat"
            job.pending_messages -= 1
            current = next(message for message in job.messages if message["id"] == request.id)
            current["status"] = "running"
            history = [
                {"role": message["role"], "text": message["text"]}
                for message in job.messages
                if message["id"] != request.id and message.get("status") == "done"
            ]
        try:
            if job.cancel.is_set():
                raise ValueError("本轮对话已取消")
            if not job.result or not job.result.note_workspace or not job.result.detailed_notes or job.options is None:
                raise ValueError("提取尚未成功，请完成课程整理后重新发送要求")
            with job.locked():
                job.status, job.stage, job.message, job.error = "running", "对话", "正在处理你的要求", ""
                self.save_job(job)
            if getattr(self, "queue", None):
                inputs = ChatInput(
                    job.result.note_workspace,
                    job.result.detailed_notes,
                    request.text,
                    history,
                    list((job.out_dir / "frames").rglob("*")),
                )
                executor = QueueExecutor(
                    self.queue,
                    job.id,
                    owner_id=job.owner or None,
                    run_id=f"{job.run_id}:{request.id}",
                    stop_event=getattr(self, "_closing", None),
                )
                reply = executor.run(
                    ChatStage(), StageContext(job.options, cancel=job.cancel, on_output=job.emit), inputs
                )
            else:
                reply = continue_notes(
                    job.result.note_workspace,
                    request.text,
                    history,
                    job.result.detailed_notes,
                    job.options.llm,
                    job.options.agent,
                    job.emit,
                    job.cancel,
                )
            job.files = _collect_files(job.result)
            if reply.updated:
                job.revision += 1
                self.record_version(job, f"对话：{request.text[:40]}")
        except Suspended:
            with job.locked():
                current["status"] = "queued"
                job.pending_messages += 1
                job.status, job.stage, job.message = "queued", "恢复", "等待服务重启后继续对话"
            self.save_job(job)
            return
        except Exception as exc:  # noqa: BLE001  本轮错误保留已有笔记，课程保持可用，后续对话仍可重试。
            with job.locked():
                cancelled = job.cancel.is_set()
                text = "本轮对话已取消" if cancelled else str(exc) or type(exc).__name__
                job.log(text)
                if not cancelled:
                    job.log(traceback.format_exc(limit=4))
                job.cancel.clear()
                current["status"] = "cancelled" if cancelled else "failed"
                usable = bool(job.result and job.result.note_workspace)
                if usable:
                    job.status, job.stage, job.error = "done", "完成", ""
                else:
                    job.status, job.error = ("cancelled" if cancelled else "failed"), text
                job.message = f"本轮对话未完成：{text}"
                job.messages.append(
                    {"id": uuid.uuid4().hex, "role": "assistant", "text": text, "status": current["status"]}
                )
        else:
            with job.locked():
                current["status"] = "done"
                job.status, job.stage, job.message = "done", "完成", "两版笔记已更新" if reply.updated else "回复完成"
                job.messages.append(
                    {
                        "id": uuid.uuid4().hex,
                        "role": "assistant",
                        "text": reply.text,
                        "status": "done",
                        "updated": reply.updated,
                    }
                )
        self.save_job(job)


def _collect_files(result) -> list[dict[str, str]]:
    """列出可下载的产物（相对输出目录）。"""
    files: list[dict[str, str]] = []
    for path in sorted(result.output_dir.rglob("*")):
        if (
            not path.is_file()
            or path.suffix in (".wav", ".part", ".lock")
            or "agent-workspace" in path.relative_to(result.output_dir).parts
            or ".course2md" in path.relative_to(result.output_dir).parts
        ):
            continue
        kind = "text"
        if path in (result.notes, result.detailed_notes, result.brief_notes):
            kind = "notes"
        elif path.suffix == ".json":
            kind = "meta"
        elif path.suffix == ".zip":
            kind = "bundle"
        elif path.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            kind = "image"
        elif path.suffix.lower() in (".mp4", ".mkv", ".mov", ".avi", ".ts"):
            kind = "media"
        files.append(
            {
                "name": path.name,
                "rel": str(path.relative_to(result.output_dir)).replace("\\", "/"),
                "size": f"{path.stat().st_size / 1024:.0f} KB",
                "kind": kind,
            }
        )
    primary = result.notes.relative_to(result.output_dir).as_posix() if result.notes else ""
    return sorted(
        files,
        key=lambda item: (0 if item["rel"] == primary else {"notes": 1, "bundle": 2}.get(item["kind"], 3), item["rel"]),
    )

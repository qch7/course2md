"""课程与任务记录、按课次并行编排、课程对话。GPU 并发由节点的串行执行器限制。"""

from __future__ import annotations

import hashlib
import queue
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from ..config import (
    default_out_dir,
    default_worker_dir,
)
from ..core.queue import TaskQueue
from ..core.versions import NoteVersions
from ..pipeline import run_job
from ..resolvers import (
    course_identity,
)
from ..stages.executor import QueueExecutor, Suspended
from ..stages.options import JobOptions
from ..storage import LocalFileStore
from .job import ChatRequest, Job, collect_files
from .records import CourseRecord, CourseView, SessionView
from .serialization import job_from_record, job_to_record
from .sessions import existing_result
from .trash import TrashService
from .workspace_store import WorkspaceStore

if TYPE_CHECKING:
    from ..worker import LocalNodeManager, WorkerRunner

FORMATS = ("srt", "vtt", "txt", "md", "json")


T = TypeVar("T")


class JobManager:
    """不同课次并行编排，同一课次的提取与对话保持顺序。

    这里的队列是“作业层”：按课次排队。阶段怎样分配给节点由 ``core.queue.TaskQueue`` 负责，
    两层的分工见 docs/architecture.md 的“两层调度”。
    """

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
                job = job_from_record(data)
                self.jobs[job.id] = job
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.warnings.append(f"任务 {data.get('id', '')} 读取失败（{exc}）")
        self.queue = TaskQueue(self.store.db, LocalFileStore(data_dir / "objects"))
        self.trash = TrashService(self, self._closing)
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
        self.trash.start()

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
                    "existing": existing_result(Path(session["folder"])),
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
            self.store.save_job(job_to_record(job), replace_existing=replace_existing)

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
                job.files = collect_files(result)
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
            job.files = collect_files(job.result)
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

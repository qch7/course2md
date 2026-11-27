"""任务的运行时对象与相关异常。"""

from __future__ import annotations

import threading
import time
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path

from markdown_it import MarkdownIt

from ..pipeline import JobResult
from ..stages.options import JobOptions
from .records import MessageRecord

ACTIVE_STATUSES = ("queued", "running")


def _markdown_html(text: str) -> str:
    return MarkdownIt("commonmark", {"html": False}).disable("image").render(text)


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


def collect_files(result: JobResult) -> list[dict[str, str]]:
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

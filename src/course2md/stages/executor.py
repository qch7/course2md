"""执行器：决定一个阶段在哪里运行。

- :class:`LocalExecutor` 在当前进程内直接调用阶段（命令行默认）。
- :class:`QueueExecutor` 把阶段与输入写入任务队列，由具备 ``requires`` 能力的节点（内置 Worker
  或远程节点）领取执行，再取回输出产物。编排代码（:func:`course2md.pipeline.run_job`）不需要区分。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import unquote, urlparse

from markdown_it import MarkdownIt

from ..errors import rebuild_error
from .artifacts import FrameSet, Notes, TranscriptFiles
from .base import Cancelled, Stage, StageContext
from .codec import cache_key, decode, encode, options_to_dict, requires_for
from .registry import STAGE_LABELS, stage_label, stage_type

if TYPE_CHECKING:
    from threading import Event

    from ..core.queue import TaskQueue


class Executor(Protocol):
    def run(self, stage: Stage, ctx: StageContext, inputs: Any) -> Any: ...


class LocalExecutor:
    """在当前进程中依次执行阶段。开始前检查一次取消。"""

    def run(self, stage: Stage, ctx: StageContext, inputs: Any) -> Any:
        ctx.check()
        return stage.run(ctx, inputs)


class StageFailed(RuntimeError):
    """节点执行阶段失败。error_type 为节点上的异常类名。"""

    def __init__(self, message: str, error_type: str = "") -> None:
        super().__init__(message)
        self.error_type = error_type


class Suspended(RuntimeError):
    """Hub 正在退出；节点任务与已完成产物留待下次接续。"""


class QueueExecutor:
    """job_id 用于关联任务；reuse 中的阶段在缓存键相同时直接复用之前的输出。

    声明 ``pinned_to_anchor`` 的阶段由执行 ``anchors_node`` 阶段的同一节点运行（需要读取原视频）。
    """

    def __init__(
        self,
        queue: TaskQueue,
        job_id: str,
        reuse: set[str] | frozenset[str] = frozenset(),
        poll_interval: float = 0.2,
        wait_hint_after: float = 3.0,
        owner_id: str | None = None,
        run_id: str = "",
        stop_event: Event | None = None,
    ) -> None:
        self.queue = queue
        self.job_id = job_id
        self.reuse = set(reuse)
        self.owner_id = owner_id
        self.run_id = run_id
        self.stop_event = stop_event
        self.poll_interval = poll_interval
        self.wait_hint_after = wait_hint_after
        self.media_worker: str | None = None  # 执行 anchors_node 阶段的节点
        self.task_ids: list[str] = []

    def run(self, stage: Stage, ctx: StageContext, inputs: Any) -> Any:
        ctx.check()
        options = options_to_dict(ctx.options)
        encoded = encode(inputs)
        key = cache_key(stage.name, options, encoded)
        existing = self.queue.find_for_run(self.job_id, self.run_id, key)
        if existing is not None and existing["status"] == "done" and not self._usable(existing):
            existing = None
        if existing is not None:
            task = self._wait(existing["id"], ctx, existing["requires"], existing["affinity"])
            for warning in task["warnings"]:
                ctx.warn(warning)
            self._remember_anchor(stage.name, task)
            return decode(self.queue.output(task))
        if stage.name in self.reuse:
            previous = self.queue.find_reusable(key)
            if previous is not None and self._usable(previous):
                ctx.progress(
                    stage_label(stage.name),
                    previous["progress"]["fraction"],
                    f"复用之前的{stage_label(stage.name)}结果",
                    None,
                )
                for warning in previous["warnings"]:
                    ctx.warn(warning)
                self._remember_anchor(stage.name, previous)
                return decode(self.queue.output(previous))
        declared = stage_type(stage.name)
        affinity = self.media_worker if declared.pinned_to_anchor else None
        requires = requires_for(stage.name, ctx.options, inputs)
        if affinity and not self.queue.capable_worker_online(requires, affinity):
            # 原节点当前不可用时，能解析来源的其它节点可重新下载视频。
            requires = sorted(set(requires + declared.fallback_requires(ctx.options)))
            affinity = None
            label = declared.display_name()
            ctx.progress(label, 0.56, f"原节点当前无法执行{label}，等待其它节点补下载后执行", None)
        task_id = self.queue.enqueue(
            self.job_id,
            stage.name,
            {"options": options, "inputs": encoded, "run_id": self.run_id},
            requires=requires,
            cache_key=key,
            affinity=affinity,
            owner_id=self.owner_id,
        )
        self.task_ids.append(task_id)
        task = self._wait(task_id, ctx, requires, affinity)
        for warning in task["warnings"]:
            ctx.warn(warning)
        self._remember_anchor(stage.name, task)
        return decode(self.queue.output(task))

    def _remember_anchor(self, name: str, task: dict[str, Any]) -> None:
        if stage_type(name).anchors_node:
            self.media_worker = task["worker_id"]

    def _usable(self, task: dict[str, Any]) -> bool:
        try:
            return _cached_files_exist(decode(self.queue.output(task)))
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _wait(self, task_id: str, ctx: StageContext, requires: list[str], affinity: str | None) -> dict[str, Any]:
        progress_seq, event_id, started, hinted = 0, 0, time.monotonic(), False
        while True:
            if self.stop_event is not None and self.stop_event.is_set():
                raise Suspended()
            self.queue.reap()
            task = self.queue.get(task_id)
            if task is None:
                raise StageFailed("阶段任务记录丢失")
            if (
                task["status"] == "pending"
                and task["affinity"]
                and stage_type(task["stage"]).pinned_to_anchor
                and not self.queue.capable_worker_online(task["requires"], task["affinity"])
            ):
                requires = stage_type(task["stage"]).fallback_requires(ctx.options)
                self.queue.unbind(task_id, requires)
                affinity = None
            if ctx.cancel.is_set() and not task["cancel_requested"] and task["status"] in ("pending", "leased"):
                self.queue.cancel(task_id)
            if task["progress"]["seq"] != progress_seq:
                progress_seq = task["progress"]["seq"]
                info = task["progress"]
                ctx.progress(info["stage"], info["fraction"], info["message"], info["sub"])
            for seen, event in self.queue.events(task_id, event_id):
                event_id = seen
                if ctx.on_output is not None:
                    ctx.on_output(event)
            status = task["status"]
            if status == "done":
                return task
            if status == "cancelled":
                raise Cancelled(task["error"] or "任务已取消")
            if status == "failed":
                raise rebuild_task_error(task)
            if (
                status == "pending"
                and not hinted
                and time.monotonic() - started > self.wait_hint_after
                and not self.queue.capable_worker_online(requires, affinity)
            ):
                hinted = True
                needed = "、".join(requires) or "任意"
                where = "原节点" if affinity else f"具备 {needed} 能力的计算节点"
                ctx.progress("等待节点", task["progress"]["fraction"], f"等待{where}上线", None)
            time.sleep(self.poll_interval)

    def cleanup(self, ctx: StageContext, media: Any) -> None:
        if self.media_worker and any(item.remote for item in media.files):
            self.queue.enqueue(
                self.job_id,
                "cleanup",
                {
                    "options": options_to_dict(ctx.options),
                    "inputs": encode(media),
                    "run_id": self.run_id,
                },
                affinity=self.media_worker,
                owner_id=self.owner_id,
            )


def rebuild_task_error(task: dict[str, Any]) -> Exception:
    """还原为节点上的业务错误类型（见 :mod:`course2md.errors`），便于沿用原有的失败处理。"""
    # 导入定义业务错误的模块，确保其类型已登记
    from .. import agents, resolvers  # noqa: F401
    from ..refine import llm  # noqa: F401

    message, error_type = task["error"] or "阶段执行失败", task["error_type"]
    return rebuild_error(error_type, message) or StageFailed(message, error_type)


def _cached_files_exist(output: Any) -> bool:
    """转写正文保存在 JSON 中，可不依赖原视频；截图、笔记等必须仍可读取。"""
    if isinstance(output, list):
        return all(_cached_files_exist(item) for item in output)
    if isinstance(output, FrameSet):
        return all(frame.path.is_file() for frame in output.frames) and (output.index is None or output.index.is_file())
    if isinstance(output, TranscriptFiles):
        return all(path.is_file() for path in output.files.values()) and (
            output.meta_path is None or output.meta_path.is_file()
        )
    if isinstance(output, Notes):
        if not output.detailed.is_file() or not output.brief.is_file():
            return False
        if output.workspace is not None and (
            not (output.workspace / "context.json").is_file()
            or not any(
                (output.workspace / name).is_file()
                for name in ("lesson.current.json", "lesson.reviewed.json", "lesson.json")
            )
        ):
            return False
        markdown = MarkdownIt("commonmark")
        for note in (output.detailed, output.brief):
            for block in markdown.parse(note.read_text(encoding="utf-8")):
                for token in block.children or []:
                    if token.type == "image":
                        address = urlparse(str(token.attrGet("src") or ""))
                        if not address.scheme and not address.netloc:
                            path = note.parent / Path(unquote(address.path))
                            if not path.is_file():
                                return False
    return True


__all__ = ["STAGE_LABELS", "Executor", "LocalExecutor", "QueueExecutor", "StageFailed", "Suspended", "stage_label"]

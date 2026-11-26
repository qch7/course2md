"""节点执行循环：领取任务 → 续约 → 执行阶段 → 上传输出。

内置 Worker 直接操作 Hub 的任务队列（:class:`course2md.core.queue.TaskQueue`），
独立节点通过 HTTP 访问（:mod:`.client`）；两者满足同一组方法（:class:`Transport`）。
"""

from __future__ import annotations

import platform
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from typing import Any, Protocol

from .. import __version__
from ..core.queue import ClaimedTask, LeaseLost
from ..stages.base import Cancelled, StageContext
from ..stages.codec import decode, encode, options_from_dict, output_model
from ..stages.registry import create_stage


class Transport(Protocol):
    def register_worker(
        self, worker_id: str, name: str, capabilities: list[str] | set[str], kind: str = ..., version: str = ...
    ) -> None: ...
    def touch_worker(self, worker_id: str) -> None: ...
    def leave(self, worker_id: str) -> None: ...
    def claim(
        self, worker_id: str, capabilities: set[str] | list[str], lease_seconds: float = ...
    ) -> ClaimedTask | None: ...
    def renew(self, task_id: str, worker_id: str, lease_seconds: float = ...) -> bool: ...
    def release(self, task_id: str, worker_id: str) -> None: ...
    def progress(
        self, task_id: str, worker_id: str, stage: str, fraction: float, message: str, sub: float | None = ...
    ) -> None: ...
    def add_event(self, task_id: str, event: dict[str, Any]) -> None: ...
    def complete(
        self, task_id: str, worker_id: str, output: Any, warnings: list[str] | None = ..., model: str = ...
    ) -> str: ...
    def fail(
        self,
        task_id: str,
        worker_id: str,
        error: str,
        error_type: str = ...,
        cancelled: bool = ...,
        warnings: list[str] | None = ...,
    ) -> None: ...


class WorkerRunner:
    """kind：builtin 或 remote。prepare / finish 是执行前后的钩子（远程节点用来下载输入、上传文件）。"""

    def __init__(
        self,
        transport: Transport,
        capabilities: set[str],
        worker_id: str | None = None,
        name: str = "",
        kind: str = "builtin",
        lease_seconds: float = 60.0,
        poll_interval: float = 1.0,
        progress_interval: float = 0.25,
        resources: dict[str, Any] | None = None,
        exec_lock: threading.Lock | None = None,
        on_task_start: Callable[[ClaimedTask], None] | None = None,
        on_task_end: Callable[[ClaimedTask, str, str], None] | None = None,
    ) -> None:
        self.transport = transport
        self.capabilities = set(capabilities)
        self.worker_id = worker_id or f"{kind}-{uuid.uuid4().hex[:12]}"
        self.name = name or platform.node() or self.worker_id
        self.kind = kind
        self.lease_seconds = lease_seconds
        self.poll_interval = poll_interval
        self.progress_interval = progress_interval
        # 允许同机多个节点连接共享已加载的模型资源与执行互斥锁，避免重复占显存
        self.resources: dict[str, Any] = resources if resources is not None else {}
        self._exec_lock = exec_lock
        self._on_task_start = on_task_start
        self._on_task_end = on_task_end
        self.current: str | None = None
        self._current_cancel: threading.Event | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期
    def register(self) -> None:
        self.transport.register_worker(
            self.worker_id, self.name, sorted(self.capabilities), kind=self.kind, version=__version__
        )

    def start(self) -> WorkerRunner:
        self.register()
        self._thread = threading.Thread(target=self.run_forever, name=f"worker-{self.worker_id}", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 15) -> None:
        self._stop.set()
        if self._current_cancel is not None:
            self._current_cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        try:
            self.transport.leave(self.worker_id)
        except Exception:  # noqa: BLE001  离线标记失败时依靠心跳超时
            pass

    def on_maintenance(self) -> None:
        """低频空闲维护钩子（默认每小时触发一次），子类可覆盖以清理本地过期缓存。"""

    def run_forever(self) -> None:
        last_touch = 0.0
        last_maintenance = 0.0
        while not self._stop.is_set():
            try:
                now = time.monotonic()
                if now - last_touch > 20:
                    self.transport.touch_worker(self.worker_id)
                    last_touch = now
                if now - last_maintenance > 3600:
                    self.on_maintenance()
                    last_maintenance = now
                if self.run_once():
                    continue
            except Exception as exc:  # noqa: BLE001  网络或数据库暂时不可用时稍后重试
                print(f"[worker] {exc}")
            self._stop.wait(self.poll_interval)

    def run_once(self) -> bool:
        if self._exec_lock is not None and self._exec_lock.locked():
            return False
        task = self.transport.claim(self.worker_id, self.capabilities, self.lease_seconds)
        if task is None:
            return False
        self.execute(task)
        return True

    # ------------------------------------------------------------ 执行
    def prepare(self, task: ClaimedTask, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    def finish(self, task: ClaimedTask, output: Any) -> Any:
        return output

    def execute(self, task: ClaimedTask) -> None:
        self.current = task.id
        if self._on_task_start is not None:
            self._on_task_start(task)
        cancel = threading.Event()
        self._current_cancel = cancel
        if self._stop.is_set():
            cancel.set()
        lost = threading.Event()
        done = threading.Event()

        def heartbeat() -> None:
            while not done.wait(max(1.0, self.lease_seconds / 3)):
                try:
                    if self.transport.renew(task.id, self.worker_id, self.lease_seconds):
                        cancel.set()
                    self.transport.touch_worker(self.worker_id)
                except LeaseLost:
                    lost.set()
                    cancel.set()
                    return
                except Exception as exc:  # noqa: BLE001  暂时失败，下次再续
                    print(f"[worker] 续约失败：{exc}")

        last: dict[str, Any] = {"time": 0.0, "stage": ""}

        def report(stage: str, fraction: float, message: str, sub: float | None = None) -> None:
            now = time.monotonic()
            if stage == last["stage"] and now - last["time"] < self.progress_interval:
                return
            last.update(time=now, stage=stage)
            try:
                self.transport.progress(task.id, self.worker_id, stage, fraction, message, sub)
            except Exception:  # noqa: BLE001  进度丢失不影响执行
                pass

        def emit(event: dict[str, Any]) -> None:
            try:
                self.transport.add_event(task.id, event)
            except Exception:  # noqa: BLE001
                pass

        beat = threading.Thread(target=heartbeat, daemon=True)
        beat.start()
        warnings: list[str] = []
        outcome = "completed"
        error_text = ""
        try:
            payload = self.prepare(task, task.payload)
            options = options_from_dict(payload["options"])
            ctx = StageContext(options=options, progress=report, cancel=cancel, on_output=emit, warnings=warnings)
            lock = self._exec_lock
            acquired = False
            try:
                if lock is not None:
                    while not acquired:
                        ctx.check()
                        acquired = lock.acquire(timeout=0.2)
                stage = create_stage(task.stage, **self.resources)
                ctx.check()
                output = stage.run(ctx, decode(payload["inputs"]))
                self.resources.update(stage.resources())
            finally:
                if acquired and lock is not None:
                    lock.release()
            output = self.finish(task, output)
            ctx.check()
            if not lost.is_set():
                self.transport.complete(
                    task.id, self.worker_id, encode(output), warnings, model=output_model(task.stage, options)
                )
            else:
                outcome = "released"
        except Cancelled as exc:
            outcome = "released" if self._stop.is_set() else "cancelled"
            error_text = str(exc)
            if not lost.is_set():
                if self._stop.is_set():
                    self.transport.release(task.id, self.worker_id)
                else:
                    self.transport.fail(
                        task.id, self.worker_id, str(exc), "Cancelled", cancelled=True, warnings=warnings
                    )
        except LeaseLost:
            outcome = "released"
            done.set()
        except Exception as exc:  # noqa: BLE001  失败原因交给 Hub 展示
            outcome = "released" if self._stop.is_set() else "failed"
            error_text = str(exc) or type(exc).__name__
            traceback.print_exc()
            if not lost.is_set():
                try:
                    if self._stop.is_set():
                        self.transport.release(task.id, self.worker_id)
                    else:
                        self.transport.fail(
                            task.id,
                            self.worker_id,
                            error_text,
                            type(exc).__name__,
                            warnings=warnings,
                        )
                except LeaseLost:
                    pass
        finally:
            done.set()
            beat.join(timeout=5)
            self.current = None
            self._current_cancel = None
            if self._on_task_end is not None:
                self._on_task_end(task, outcome, error_text)

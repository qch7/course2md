"""独立计算节点：通过 HTTP 连接 Hub。

- :class:`HttpTransport` 实现与 :class:`course2md.core.queue.TaskQueue` 相同的节点方法。
- :class:`RemoteWorker` 在本地工作目录（``<work_dir>/<job_id>``）执行阶段：执行前下载需要的 Hub 文件，
  执行后上传新产生的文件，Hub 再写回自己的任务目录。下载的视频与音频留在节点上。
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from ..core.queue import ClaimedTask, LeaseLost
from ..storage import file_digest
from ..util.job_refs import SCHEME, from_job_refs, job_refs, resolve_relative, to_job_refs
from .runner import WorkerRunner

# 留在节点本地、不回传 Hub 的目录（原视频与中间音频）
LOCAL_ONLY_DIRS = ("media", "audio")


class HubError(RuntimeError):
    pass


class AuthError(HubError):
    """节点令牌无效或已停用，应停止重试并提示重新生成令牌。"""


class HttpTransport:
    def __init__(self, hub: str, token: str, timeout: float = 30.0, session: requests.Session | None = None) -> None:
        self.base = hub.rstrip("/") + "/"
        self.session = session or requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.timeout = timeout
        self.worker_id = ""

    def _url(self, path: str) -> str:
        return urljoin(self.base, "api/v1/worker/" + path.lstrip("/"))

    def _call(self, method: str, path: str, **kwargs) -> Any:
        response = self.session.request(method, self._url(path), timeout=kwargs.pop("timeout", self.timeout), **kwargs)
        if response.status_code == 409:
            raise LeaseLost(_detail(response))
        if response.status_code == 401:
            raise AuthError("节点令牌无效或已停用，请在 Hub 的计算服务页重新生成")
        if response.status_code >= 400:
            raise HubError(f"Hub 返回 {response.status_code}：{_detail(response)}")
        return response.json() if response.content else None

    # ------------------------------------------------------------ Transport
    def register_worker(self, worker_id: str, name: str, capabilities, kind: str = "remote", version: str = "") -> None:
        data = self._call(
            "POST", "register", json={"name": name, "capabilities": sorted(capabilities), "version": version}
        )
        self.worker_id = data["worker_id"]

    def touch_worker(self, worker_id: str) -> None:
        self._call("POST", "heartbeat")

    def leave(self, worker_id: str) -> None:
        self._call("POST", "leave", timeout=5)

    def claim(self, worker_id: str, capabilities, lease_seconds: float = 60.0) -> ClaimedTask | None:
        data = self._call("POST", "claim", json={"capabilities": sorted(capabilities), "lease_seconds": lease_seconds})
        task = data.get("task")
        if not task:
            return None
        return ClaimedTask(
            task["id"], task["job_id"], task["stage"], task["payload"], task["attempts"], task["lease_until"]
        )

    def renew(self, task_id: str, worker_id: str, lease_seconds: float = 60.0) -> bool:
        return bool(self._call("POST", f"tasks/{task_id}/renew", json={"lease_seconds": lease_seconds})["cancel"])

    def release(self, task_id: str, worker_id: str) -> None:
        self._call("POST", f"tasks/{task_id}/release")

    def progress(
        self, task_id: str, worker_id: str, stage: str, fraction: float, message: str, sub: float | None = None
    ) -> None:
        self._call(
            "POST",
            f"tasks/{task_id}/progress",
            json={"stage": stage, "fraction": fraction, "message": message, "sub": sub},
        )

    def add_event(self, task_id: str, event: dict[str, Any]) -> None:
        self._call("POST", f"tasks/{task_id}/events", json=event)

    def complete(
        self,
        task_id: str,
        worker_id: str,
        output: Any,
        warnings: list[str] | None = None,
        model: str = "",
        files: dict[str, str] | None = None,
    ) -> str:
        data = self._call(
            "POST",
            f"tasks/{task_id}/complete",
            timeout=300,
            json={"output": output, "warnings": list(warnings or []), "model": model, "files": files or {}},
        )
        return data["result"]

    def fail(
        self,
        task_id: str,
        worker_id: str,
        error: str,
        error_type: str = "",
        cancelled: bool = False,
        warnings: list[str] | None = None,
    ) -> None:
        self._call(
            "POST",
            f"tasks/{task_id}/fail",
            json={"error": error, "error_type": error_type, "cancelled": cancelled, "warnings": list(warnings or [])},
        )

    # ------------------------------------------------------------ 文件
    def download(self, task_id: str, relative: str, target: Path) -> bool:
        headers = {"If-None-Match": f'"{file_digest(target)}"'} if target.is_file() else {}
        with self.session.get(
            self._url(f"tasks/{task_id}/files"),
            params={"rel": relative},
            headers=headers,
            stream=True,
            timeout=self.timeout,
        ) as response:
            if response.status_code == 304:
                return True
            if response.status_code == 404:
                return False
            if response.status_code >= 400:
                raise HubError(f"下载 {relative} 失败：{_detail(response)}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.part")
            try:
                with temp.open("wb") as handle:
                    for block in response.iter_content(1024 * 1024):
                        handle.write(block)
                expected = response.headers.get("X-Content-SHA256")
                if expected and file_digest(temp) != expected:
                    raise HubError(f"下载 {relative} 的内容摘要不符，请重试")
                os.replace(temp, target)
            finally:
                temp.unlink(missing_ok=True)
            return True

    def upload(self, path: Path, digest: str) -> None:
        if self.session.head(self._url(f"blobs/{digest}"), timeout=self.timeout).status_code == 200:
            return
        with path.open("rb") as handle:
            self._call(
                "PUT", f"blobs/{digest}", data=handle, timeout=600, headers={"Content-Type": "application/octet-stream"}
            )


def _detail(response: requests.Response) -> str:
    try:
        return str(response.json().get("error") or response.text[:200])
    except ValueError:
        return response.text[:200]


class RemoteWorker(WorkerRunner):
    """独立节点。work_dir 下每个任务一个目录。"""

    def __init__(
        self,
        transport: HttpTransport,
        capabilities: set[str],
        work_dir: Path,
        name: str = "",
        lease_seconds: float = 60.0,
        poll_interval: float = 2.0,
        resources: dict[str, Any] | None = None,
        exec_lock: threading.Lock | None = None,
        on_task_start: Callable[[ClaimedTask], None] | None = None,
        on_task_end: Callable[[ClaimedTask, str, str], None] | None = None,
    ) -> None:
        super().__init__(
            transport,
            capabilities,
            worker_id="pending",
            name=name,
            kind="remote",
            lease_seconds=lease_seconds,
            poll_interval=poll_interval,
            resources=resources,
            exec_lock=exec_lock,
            on_task_start=on_task_start,
            on_task_end=on_task_end,
        )
        self.http = transport
        self.work_dir = work_dir
        self._before: dict[str, dict[str, str]] = {}

    def register(self) -> None:
        super().register()
        self.worker_id = self.http.worker_id

    def job_dir(self, task: ClaimedTask) -> Path:
        return self.work_dir / task.job_id

    def prepare(self, task: ClaimedTask, payload: dict[str, Any]) -> dict[str, Any]:
        root = self.job_dir(task)
        root.mkdir(parents=True, exist_ok=True)
        for relative in payload.get("input_files", sorted(set(job_refs(payload["inputs"])))):
            target = resolve_relative(root, relative)
            if Path(relative).parts[0] in LOCAL_ONLY_DIRS:
                continue
            if not self.http.download(task.id, relative, target):
                raise HubError(f"Hub 缺少输入文件：{relative}，请重新运行前置阶段")
        self._before[task.id] = self._files(root)
        local = from_job_refs(payload, root)
        local["options"]["out_dir"] = str(root)
        return local

    def finish(self, task: ClaimedTask, output: Any) -> Any:
        return output

    def execute(self, task: ClaimedTask) -> None:
        # 让 complete 携带上传的文件清单：包装 transport.complete
        root = self.job_dir(task)
        original = self.http.complete

        def complete(task_id: str, worker_id: str, output: Any, warnings=None, model: str = "") -> str:
            files = self.upload_outputs(task_id, root)
            return original(task_id, worker_id, to_job_refs(output, root), warnings, model, files=files)

        self.transport.complete = complete  # type: ignore[method-assign]
        try:
            super().execute(task)
        finally:
            self.transport.complete = original  # type: ignore[method-assign]
            self._before.pop(task.id, None)

    @staticmethod
    def _files(root: Path) -> dict[str, str]:
        return {
            path.relative_to(root).as_posix(): file_digest(path)
            for path in sorted(root.rglob("*"))
            if path.is_file()
            and not path.name.endswith(".part")
            and path.relative_to(root).parts[0] not in LOCAL_ONLY_DIRS
        }

    def upload_outputs(self, task_id: str, root: Path) -> dict[str, str]:
        """上传本阶段新产生或修改的文件（不含 media、audio），返回 相对路径 → 摘要。"""
        before = self._before.get(task_id, {})
        files: dict[str, str] = {}
        for relative, digest in self._files(root).items():
            if before.get(relative) == digest:
                continue
            self.http.upload(root / relative, digest)
            files[relative] = digest
        return files


__all__ = ["SCHEME", "AuthError", "HttpTransport", "HubError", "RemoteWorker"]

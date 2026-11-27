"""网状计算服务：管理本机对外连接的多个远程 Hub。

一台运行 course2md 的电脑既可以作为 Hub 接收其它节点连入，也可以同时作为计算节点
连入多个外部 Hub；所有对外连接与本机内置节点共享同一把执行互斥锁与模型资源缓存，
避免重复加载转写模型或争抢显存。
"""

from __future__ import annotations

import hashlib
import platform
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..config import default_worker_dir, env
from ..core.queue import ClaimedTask
from ..util.jsonfile import read_json_object, write_json_private
from .capabilities import detect_capabilities
from .client import AuthError, HttpTransport, HubError, RemoteWorker

BACKOFF_SECONDS = (2.0, 5.0, 15.0, 30.0)
HEARTBEAT_INTERVAL = 20.0
MAX_LOGS = 40
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"})


def normalize_hub_url(raw: str) -> str:
    """规范化 Hub 地址并校验协议与主机名。"""
    text = raw.strip().rstrip("/")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Hub 地址必须以 http:// 或 https:// 开头并包含有效主机名")
    return text


def _origin_key(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _canonical_url_key(url: str) -> str:
    parsed = urlparse(url)
    scheme, host, port = _origin_key(url)
    default_port = 443 if scheme == "https" else 80
    netloc = host if port == default_port else f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}"


def token_hint(token: str) -> str:
    """返回脱敏后的令牌预览，避免把完整密钥暴露给前端。"""
    text = token.strip()
    if not text:
        return ""
    if len(text) <= 10:
        return "****"
    return f"{text[:4]}…{text[-4:]}"


def _env_signature(hub_url: str, token: str) -> str:
    return hashlib.sha256(f"{_canonical_url_key(hub_url)}\n{token}".encode()).hexdigest()


def _clean_without(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if not values:
        return []
    seen: list[str] = []
    for item in values:
        name = str(item).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _default_work_dir(configured: str, fallback: Path) -> Path:
    text = configured.strip() or env("COURSE2MD_WORKER_DIR").strip()
    return Path(text).expanduser().resolve() if text else fallback.resolve()


@dataclass
class HubLinkConfig:
    id: str
    hub_url: str
    token: str
    name: str = ""
    work_dir: str = ""
    auto_connect: bool = True
    no_agents: bool = False
    without: list[str] = field(default_factory=list)
    source: str = "manual"
    created_at: float = field(default_factory=time.time)

    def to_storage(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "hub_url": self.hub_url,
            "token": self.token,
            "name": self.name,
            "work_dir": self.work_dir,
            "auto_connect": self.auto_connect,
            "no_agents": self.no_agents,
            "without": list(self.without),
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> HubLinkConfig | None:
        raw_url = str(data.get("hub_url", "")).strip()
        if not raw_url:
            return None
        try:
            hub_url = normalize_hub_url(raw_url)
        except ValueError:
            return None
        return cls(
            id=str(data.get("id") or f"link-{uuid.uuid4().hex[:12]}"),
            hub_url=hub_url,
            token=str(data.get("token", "")),
            name=str(data.get("name", "")),
            work_dir=str(data.get("work_dir", "")),
            auto_connect=bool(data.get("auto_connect", True)),
            no_agents=bool(data.get("no_agents", False)),
            without=_clean_without(data.get("without")),
            source="env" if data.get("source") == "env" else "manual",
            created_at=float(data.get("created_at") or time.time()),
        )


WorkerFactory = Callable[
    [
        HubLinkConfig,
        set[str],
        Path,
        dict[str, Any],
        threading.Lock,
        Callable[[ClaimedTask], None],
        Callable[[ClaimedTask, str, str], None],
    ],
    RemoteWorker,
]


def _default_worker_factory(
    cfg: HubLinkConfig,
    capabilities: set[str],
    work_dir: Path,
    resources: dict[str, Any],
    exec_lock: threading.Lock,
    on_task_start: Callable[[ClaimedTask], None],
    on_task_end: Callable[[ClaimedTask, str, str], None],
) -> RemoteWorker:
    return RemoteWorker(
        HttpTransport(cfg.hub_url, cfg.token),
        capabilities,
        work_dir,
        name=cfg.name or platform.node() or cfg.id,
        resources=resources,
        exec_lock=exec_lock,
        on_task_start=on_task_start,
        on_task_end=on_task_end,
    )


class _HubLinkSession:
    """单条对外 Hub 连接的运行态与后台线程。"""

    def __init__(
        self,
        config: HubLinkConfig,
        default_work_dir: Path,
        resources: dict[str, Any],
        exec_lock: threading.Lock,
        factory: WorkerFactory,
    ) -> None:
        self.config = config
        self._default_work_dir = default_work_dir
        self._resources = resources
        self._exec_lock = exec_lock
        self._factory = factory
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._worker: RemoteWorker | None = None

        self.status: str = "disconnected"
        self.worker_id: str = ""
        self.capabilities: list[str] = sorted(self._compute_capabilities())
        self.connected_at: float | None = None
        self.last_heartbeat: float | None = None
        self.last_error: str = ""
        self.retry_at: float | None = None
        self.current_task: dict[str, Any] | None = None
        self.completed_tasks: int = 0
        self.failed_tasks: int = 0
        self.logs: deque[str] = deque(maxlen=MAX_LOGS)

    def _compute_capabilities(self) -> set[str]:
        caps = detect_capabilities(hub=False, agents=not self.config.no_agents)
        caps -= set(self.config.without)
        return caps

    def _log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.logs.append(f"[{stamp}] {message}")

    def is_active(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop = threading.Event()
            self.status = "connecting"
            self.last_error = ""
            self.retry_at = None
            self.capabilities = sorted(self._compute_capabilities())
            thread = threading.Thread(
                target=self._run,
                name=f"hub-link-{self.config.id}",
                daemon=True,
            )
            self._thread = thread
        thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        with self._lock:
            self._stop.set()
            worker = self._worker
            thread = self._thread
        if worker is not None:
            # 先触发当前任务取消，避免 join 等待漫长阶段完成
            worker._stop.set()
            if worker._current_cancel is not None:
                worker._current_cancel.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        with self._lock:
            self._worker = None
            if not (self._thread is not None and self._thread.is_alive()):
                self._thread = None
                self.status = "disconnected"
                self.current_task = None
                self.retry_at = None

    def _on_task_start(self, task: ClaimedTask) -> None:
        with self._lock:
            self.status = "busy"
            self.current_task = {
                "id": task.id,
                "job_id": task.job_id,
                "stage": task.stage,
                "started_at": time.time(),
            }
        self._log(f"开始执行阶段 {task.stage}（任务 {task.job_id}）")

    def _on_task_end(self, task: ClaimedTask, outcome: str, error: str) -> None:
        with self._lock:
            self.current_task = None
            if not self._stop.is_set() and self.status == "busy":
                self.status = "online"
            if outcome == "completed":
                self.completed_tasks += 1
            elif outcome == "failed":
                self.failed_tasks += 1
        if outcome == "completed":
            self._log(f"完成阶段 {task.stage}（任务 {task.job_id}）")
        elif outcome == "failed":
            self._log(f"阶段 {task.stage} 失败：{error}")
        elif outcome == "cancelled":
            self._log(f"阶段 {task.stage} 已取消")
        else:
            self._log(f"阶段 {task.stage} 已归还给 Hub")

    def _run(self) -> None:  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
        backoff_idx = 0
        while not self._stop.is_set():
            caps = self._compute_capabilities()
            work_dir = _default_work_dir(self.config.work_dir, self._default_work_dir)
            worker = self._factory(
                self.config,
                caps,
                work_dir,
                self._resources,
                self._exec_lock,
                self._on_task_start,
                self._on_task_end,
            )
            with self._lock:
                self._worker = worker
                self.capabilities = sorted(caps)
                if self.status != "reconnecting":
                    self.status = "connecting"

            try:
                worker.register()
            except AuthError as exc:
                with self._lock:
                    self._worker = None
                    self.status = "auth_failed"
                    self.last_error = str(exc)
                    self.retry_at = None
                self._log(f"鉴权失败：{exc}")
                return
            except (HubError, requests.RequestException, OSError) as exc:
                self._schedule_retry(str(exc), backoff_idx)
                backoff_idx = min(backoff_idx + 1, len(BACKOFF_SECONDS) - 1)
                continue
            except Exception as exc:  # noqa: BLE001  后台节点线程边界兜底，避免静默崩溃
                self._schedule_retry(str(exc) or type(exc).__name__, backoff_idx)
                backoff_idx = min(backoff_idx + 1, len(BACKOFF_SECONDS) - 1)
                continue

            now = time.time()
            backoff_idx = 0
            with self._lock:
                self.worker_id = worker.worker_id
                self.status = "online"
                self.connected_at = now
                self.last_heartbeat = now
                self.last_error = ""
                self.retry_at = None
            self._log(f"已连接 {self.config.hub_url}（节点 {worker.worker_id}）")

            last_touch = time.monotonic()
            need_reconnect = False
            try:
                while not self._stop.is_set():
                    try:
                        if time.monotonic() - last_touch >= HEARTBEAT_INTERVAL:
                            worker.transport.touch_worker(worker.worker_id)
                            last_touch = time.monotonic()
                            with self._lock:
                                self.last_heartbeat = time.time()
                        if worker.run_once():
                            with self._lock:
                                self.last_heartbeat = time.time()
                            continue
                    except AuthError as exc:
                        with self._lock:
                            self.status = "auth_failed"
                            self.last_error = str(exc)
                            self.retry_at = None
                        self._log(f"鉴权失效：{exc}")
                        return
                    except (HubError, requests.RequestException, OSError) as exc:
                        need_reconnect = True
                        self._schedule_retry(str(exc), backoff_idx)
                        backoff_idx = min(backoff_idx + 1, len(BACKOFF_SECONDS) - 1)
                        break
                    except Exception as exc:  # noqa: BLE001  后台节点循环异常时重连，避免线程死亡
                        need_reconnect = True
                        self._schedule_retry(str(exc) or type(exc).__name__, backoff_idx)
                        backoff_idx = min(backoff_idx + 1, len(BACKOFF_SECONDS) - 1)
                        break
                    self._stop.wait(worker.poll_interval)
            finally:
                if self._stop.is_set() and worker.worker_id:
                    try:
                        worker.transport.leave(worker.worker_id)
                    except Exception:  # noqa: BLE001  离线通知失败时依靠 Hub 心跳超时
                        pass
                with self._lock:
                    self._worker = None
                    self.current_task = None
                    if self._stop.is_set():
                        self.status = "disconnected"
                        self.retry_at = None
            if not need_reconnect:
                return

    def _schedule_retry(self, reason: str, backoff_idx: int) -> None:
        delay = BACKOFF_SECONDS[backoff_idx]
        with self._lock:
            self._worker = None
            self.status = "reconnecting"
            self.last_error = reason
            self.retry_at = time.time() + delay
        self._log(f"连接中断：{reason}（{int(delay)} 秒后重试）")
        self._stop.wait(delay)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.config.id,
                "hub_url": self.config.hub_url,
                "name": self.config.name or platform.node() or self.config.id,
                "work_dir": str(_default_work_dir(self.config.work_dir, self._default_work_dir)),
                "has_token": bool(self.config.token.strip()),
                "token_hint": token_hint(self.config.token),
                "auto_connect": self.config.auto_connect,
                "no_agents": self.config.no_agents,
                "without": list(self.config.without),
                "source": self.config.source,
                "status": self.status,
                "worker_id": self.worker_id,
                "capabilities": list(self.capabilities),
                "connected_at": self.connected_at,
                "last_heartbeat": self.last_heartbeat,
                "last_error": self.last_error,
                "retry_at": self.retry_at,
                "current_task": dict(self.current_task) if self.current_task else None,
                "completed_tasks": self.completed_tasks,
                "failed_tasks": self.failed_tasks,
                "logs": list(self.logs),
                "created_at": self.config.created_at,
            }


class HubLinkManager:
    """管理本机对外连接的所有 Hub，支持持久化、.env 自动发现与共享执行锁。"""

    def __init__(
        self,
        state_path: Path,
        resources: dict[str, Any] | None = None,
        exec_lock: threading.Lock | None = None,
        default_work_dir: Path | None = None,
        worker_factory: WorkerFactory | None = None,
    ) -> None:
        self.state_path = state_path
        self.resources: dict[str, Any] = resources if resources is not None else {}
        self.exec_lock: threading.Lock = exec_lock if exec_lock is not None else threading.Lock()
        self.default_work_dir = (default_work_dir or default_worker_dir()).resolve()
        self._factory: WorkerFactory = worker_factory or _default_worker_factory
        self._lock = threading.RLock()
        self._local_origins: set[tuple[str, str, int]] = set()
        self._configs: dict[str, HubLinkConfig] = {}
        self._sessions: dict[str, _HubLinkSession] = {}
        self._env_signatures: list[str] = []
        self._load()

    def set_local_origins(self, origins: set[str]) -> None:
        parsed: set[tuple[str, str, int]] = set()
        for origin in origins:
            try:
                scheme, host, port = _origin_key(normalize_hub_url(origin))
            except ValueError:
                continue
            parsed.add((scheme, host, port))
            if host in LOOPBACK_HOSTS:
                for alias in ("127.0.0.1", "localhost", "::1"):
                    parsed.add((scheme, alias, port))
        with self._lock:
            self._local_origins = parsed

    def local_origin_urls(self) -> set[str]:
        """本机 Hub 的来源（含回环别名）的 URL 形式，用于拒绝把互传或链接指向自己。"""
        with self._lock:
            return {f"{scheme}://{host}:{port}" for scheme, host, port in self._local_origins}

    def _is_self_url(self, hub_url: str) -> bool:
        return _origin_key(hub_url) in self._local_origins

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        raw = read_json_object(self.state_path)
        if raw is None:
            return
        self._env_signatures = [str(item) for item in raw.get("env_signatures", []) if item]
        for entry in raw.get("links", []):
            if not isinstance(entry, dict):
                continue
            cfg = HubLinkConfig.from_storage(entry)
            if cfg is None:
                continue
            self._configs[cfg.id] = cfg
            self._sessions[cfg.id] = self._make_session(cfg)

    def _save(self) -> None:
        write_json_private(
            self.state_path,
            {
                "version": 1,
                "env_signatures": self._env_signatures[-20:],
                "links": [cfg.to_storage() for cfg in self._configs.values()],
            },
            prefix=".hub-links-",
        )

    def _make_session(self, cfg: HubLinkConfig) -> _HubLinkSession:
        return _HubLinkSession(cfg, self.default_work_dir, self.resources, self.exec_lock, self._factory)

    # ------------------------------------------------------------ .env 自动接入
    def sync_env(self) -> list[str]:
        """检测到 .env / 环境变量配置了 Hub 地址与令牌时，自动导入或同步。"""
        raw_url = env("COURSE2MD_HUB_URL").strip()
        token = env("COURSE2MD_WORKER_TOKEN").strip()
        if not raw_url or not token:
            return []
        try:
            hub_url = normalize_hub_url(raw_url)
        except ValueError:
            return []
        if self._is_self_url(hub_url):
            return []
        sig = _env_signature(hub_url, token)
        key = _canonical_url_key(hub_url)
        env_name = env("COURSE2MD_WORKER_NAME").strip()
        env_dir = env("COURSE2MD_WORKER_DIR").strip()

        with self._lock:
            existing = next((cfg for cfg in self._configs.values() if _canonical_url_key(cfg.hub_url) == key), None)
            if existing is not None:
                if sig in self._env_signatures:
                    return []
                existing.token = token
                if env_name and not existing.name:
                    existing.name = env_name
                if env_dir and not existing.work_dir:
                    existing.work_dir = env_dir
                existing.auto_connect = True
                self._env_signatures.append(sig)
                self._save()
                return [existing.id]
            if sig in self._env_signatures:
                return []
            cfg = HubLinkConfig(
                id=f"link-{uuid.uuid4().hex[:12]}",
                hub_url=hub_url,
                token=token,
                name=env_name,
                work_dir=env_dir,
                auto_connect=True,
                source="env",
            )
            self._configs[cfg.id] = cfg
            self._sessions[cfg.id] = self._make_session(cfg)
            self._env_signatures.append(sig)
            self._save()
            return [cfg.id]

    def start_auto(self) -> list[str]:
        """同步 .env 并自动连接所有已启用自动连接的 Hub。"""
        self.sync_env()
        started: list[str] = []
        with self._lock:
            candidates = list(self._configs.values())
        for cfg in candidates:
            if not cfg.auto_connect or not cfg.token.strip() or self._is_self_url(cfg.hub_url):
                continue
            session = self._sessions.get(cfg.id)
            if session is not None:
                session.start()
                started.append(cfg.id)
        return started

    # ------------------------------------------------------------ 查询与操作
    def local_capabilities(self) -> list[str]:
        return sorted(detect_capabilities(hub=False))

    def list_links(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [session.snapshot() for session in sessions]

    def get_link(self, link_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(link_id)
        return session.snapshot() if session is not None else None

    def add_link(
        self,
        hub_url: str,
        token: str,
        *,
        name: str = "",
        work_dir: str = "",
        auto_connect: bool = True,
        no_agents: bool = False,
        without: list[str] | None = None,
        connect_now: bool = True,
        allow_custom_work_dir: bool = True,
    ) -> dict[str, Any]:
        normalized = normalize_hub_url(hub_url)
        if self._is_self_url(normalized):
            raise ValueError("不能连接到本机自身地址，请填写其它 Hub 的地址")
        clean_token = token.strip()
        if not clean_token:
            raise ValueError("请填写节点令牌")
        key = _canonical_url_key(normalized)
        with self._lock:
            if any(_canonical_url_key(item.hub_url) == key for item in self._configs.values()):
                raise ValueError(f"已经添加过 {normalized}，可以直接编辑或重新连接")
            cfg = HubLinkConfig(
                id=f"link-{uuid.uuid4().hex[:12]}",
                hub_url=normalized,
                token=clean_token,
                name=name.strip(),
                work_dir=work_dir.strip() if allow_custom_work_dir else "",
                auto_connect=bool(auto_connect),
                no_agents=bool(no_agents),
                without=_clean_without(without),
                source="manual",
            )
            session = self._make_session(cfg)
            self._configs[cfg.id] = cfg
            self._sessions[cfg.id] = session
            self._save()
        if connect_now:
            session.start()
        return session.snapshot()

    def update_link(  # noqa: PLR0912  历史遗留的长函数，拆分后移除
        self,
        link_id: str,
        *,
        hub_url: str | None = None,
        token: str | None = None,
        name: str | None = None,
        work_dir: str | None = None,
        auto_connect: bool | None = None,
        no_agents: bool | None = None,
        without: list[str] | None = None,
        allow_custom_work_dir: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            cfg = self._configs.get(link_id)
            session = self._sessions.get(link_id)
            if cfg is None or session is None:
                raise KeyError(link_id)
            was_active = session.is_active()
            need_restart = False

            if hub_url is not None:
                normalized = normalize_hub_url(hub_url)
                if self._is_self_url(normalized):
                    raise ValueError("不能连接到本机自身地址，请填写其它 Hub 的地址")
                key = _canonical_url_key(normalized)
                if any(
                    item.id != link_id and _canonical_url_key(item.hub_url) == key for item in self._configs.values()
                ):
                    raise ValueError(f"已经添加过 {normalized}")
                if normalized != cfg.hub_url:
                    cfg.hub_url = normalized
                    need_restart = True
            if token is not None and token.strip() and token.strip() != cfg.token:
                cfg.token = token.strip()
                need_restart = True
            if name is not None and name.strip() != cfg.name:
                cfg.name = name.strip()
                need_restart = True
            if allow_custom_work_dir and work_dir is not None and work_dir.strip() != cfg.work_dir:
                cfg.work_dir = work_dir.strip()
                need_restart = True
            if auto_connect is not None:
                cfg.auto_connect = bool(auto_connect)
            if no_agents is not None and bool(no_agents) != cfg.no_agents:
                cfg.no_agents = bool(no_agents)
                need_restart = True
            if without is not None:
                cleaned = _clean_without(without)
                if cleaned != cfg.without:
                    cfg.without = cleaned
                    need_restart = True
            self._save()

        if need_restart and was_active:
            session.stop()
            session.start()
        elif not was_active:
            with session._lock:
                session.capabilities = sorted(session._compute_capabilities())
        return session.snapshot()

    def connect(self, link_id: str) -> dict[str, Any]:
        with self._lock:
            cfg = self._configs.get(link_id)
            session = self._sessions.get(link_id)
            if cfg is None or session is None:
                raise KeyError(link_id)
            if self._is_self_url(cfg.hub_url):
                raise ValueError("不能连接到本机自身地址")
            if not cfg.token.strip():
                raise ValueError("缺少节点令牌，请先编辑并填写令牌")
            cfg.auto_connect = True
            self._save()
        session.start()
        return session.snapshot()

    def disconnect(self, link_id: str) -> dict[str, Any]:
        with self._lock:
            cfg = self._configs.get(link_id)
            session = self._sessions.get(link_id)
            if cfg is None or session is None:
                raise KeyError(link_id)
            cfg.auto_connect = False
            self._save()
        session.stop()
        return session.snapshot()

    def delete_link(self, link_id: str) -> None:
        with self._lock:
            cfg = self._configs.pop(link_id, None)
            session = self._sessions.pop(link_id, None)
            if cfg is None or session is None:
                raise KeyError(link_id)
            self._save()
        session.stop()

    def close(self, timeout: float = 10.0) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            with session._lock:
                session._stop.set()
                worker = session._worker
            if worker is not None:
                worker._stop.set()
                if worker._current_cancel is not None:
                    worker._current_cancel.set()
        deadline = time.monotonic() + timeout
        for session in sessions:
            remaining = max(0.2, deadline - time.monotonic())
            session.stop(timeout=remaining)


__all__ = ["HubLinkConfig", "HubLinkManager", "normalize_hub_url", "token_hint"]

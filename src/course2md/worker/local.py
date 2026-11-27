"""本机节点：与 Hub 同进程运行的多个计算节点，各自声明可承担的能力。

计算服务页让用户自由创建、修改、停用本机节点。所有本机节点与对外连接的 Hub 共享同一把执行互斥锁与
模型资源缓存，所以多个节点只用于把能力分开（例如一个专做转写、一个专做整理），不会同时占用显存。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .runner import Transport, WorkerRunner

BUILTIN_ID = "builtin"
BUILTIN_NAME = "内置节点"
MAX_LOCAL_NODES = 16
MAX_NAME_LENGTH = 60

RunnerFactory = Callable[["LocalNodeConfig", set[str]], WorkerRunner]


@dataclass
class LocalNodeConfig:
    id: str
    name: str
    # auto 为 True 时随本机检测结果走（安装了新的插件或模型后自动获得对应能力），否则只承担 capabilities 中勾选的部分
    auto: bool = True
    capabilities: list[str] = field(default_factory=list)
    enabled: bool = True
    created_at: float = field(default_factory=time.time)

    def to_storage(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "auto": self.auto,
            "capabilities": list(self.capabilities),
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_storage(cls, data: dict[str, Any]) -> LocalNodeConfig | None:
        node_id = str(data.get("id", "")).strip()
        if not node_id:
            return None
        raw = data.get("capabilities")
        return cls(
            id=node_id,
            name=str(data.get("name", "")).strip()[:MAX_NAME_LENGTH] or BUILTIN_NAME,
            auto=bool(data.get("auto", True)),
            capabilities=sorted({str(item) for item in raw}) if isinstance(raw, list) else [],
            enabled=bool(data.get("enabled", True)),
            created_at=float(data.get("created_at") or time.time()),
        )


class LocalNodeManager:
    """持久化本机节点配置，并为每个启用的节点维护一个进程内 WorkerRunner。"""

    def __init__(
        self,
        state_path: Path,
        transport: Transport,
        *,
        resources: dict[str, Any],
        exec_lock: threading.Lock,
        ceiling: Callable[[], set[str]],
        poll_interval: float = 0.2,
        runner_factory: RunnerFactory | None = None,
    ) -> None:
        self.state_path = state_path
        self._transport = transport
        self._resources = resources
        self._exec_lock = exec_lock
        self._ceiling = ceiling
        self._poll_interval = poll_interval
        self._factory: RunnerFactory = runner_factory or self._default_factory
        self._lock = threading.RLock()
        self._configs: dict[str, LocalNodeConfig] = {}
        self._runners: dict[str, WorkerRunner] = {}
        self._load()

    # ------------------------------------------------------------ 持久化
    def _load(self) -> None:
        raw: Any = None
        if self.state_path.is_file():
            try:
                raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = None
        entries = raw.get("nodes", []) if isinstance(raw, dict) else []
        for entry in entries:
            cfg = LocalNodeConfig.from_storage(entry) if isinstance(entry, dict) else None
            if cfg is not None:
                self._configs[cfg.id] = cfg
        if BUILTIN_ID not in self._configs:
            # 内置节点始终存在，用户只能修改或停用，不能删除
            self._configs = {BUILTIN_ID: LocalNodeConfig(id=BUILTIN_ID, name=BUILTIN_NAME), **self._configs}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "nodes": [cfg.to_storage() for cfg in self._configs.values()]}
        data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=self.state_path.parent, prefix=".local-nodes-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(data)
            if sys.platform != "win32":
                Path(tmp_name).chmod(0o600)
            os.replace(tmp_name, self.state_path)
        finally:
            Path(tmp_name).unlink(missing_ok=True)

    # ------------------------------------------------------------ 能力
    def available_capabilities(self) -> list[str]:
        """本机当前能够承担的全部能力，节点只能在这个范围内取子集。"""
        return sorted(self._ceiling())

    def effective_capabilities(self, cfg: LocalNodeConfig, ceiling: set[str] | None = None) -> set[str]:
        # 能力检测会探测已安装的 Agent，列表接口一次只检测一遍再传进来
        available = self._ceiling() if ceiling is None else ceiling
        return available if cfg.auto else available & set(cfg.capabilities)

    def _validate_capabilities(self, capabilities: list[str]) -> list[str]:
        cleaned = sorted({str(item).strip() for item in capabilities if str(item).strip()})
        if not cleaned:
            raise ValueError("请至少选择一项能力，或改为自动跟随本机检测结果")
        unsupported = [item for item in cleaned if item not in self._ceiling()]
        if unsupported:
            raise ValueError(f"本机不具备这些能力：{'、'.join(unsupported)}")
        return cleaned

    # ------------------------------------------------------------ 运行
    def _default_factory(self, cfg: LocalNodeConfig, capabilities: set[str]) -> WorkerRunner:
        return WorkerRunner(
            self._transport,
            capabilities,
            worker_id=cfg.id,
            name=cfg.name,
            kind="builtin",
            poll_interval=self._poll_interval,
            resources=self._resources,
            exec_lock=self._exec_lock,
        )

    def _launch(self, cfg: LocalNodeConfig) -> None:
        if not cfg.enabled:
            return
        capabilities = self.effective_capabilities(cfg)
        self._runners[cfg.id] = self._factory(cfg, capabilities).start()

    def _halt(self, node_id: str) -> None:
        runner = self._runners.pop(node_id, None)
        if runner is not None:
            runner.stop()

    def start(self) -> LocalNodeManager:
        with self._lock:
            for cfg in self._configs.values():
                if cfg.id not in self._runners:
                    self._launch(cfg)
        return self

    def close(self) -> None:
        with self._lock:
            for node_id in list(self._runners):
                self._halt(node_id)

    def runner(self, node_id: str) -> WorkerRunner | None:
        with self._lock:
            return self._runners.get(node_id)

    # ------------------------------------------------------------ 查询
    def snapshot(self, cfg: LocalNodeConfig, ceiling: set[str] | None = None) -> dict[str, Any]:
        runner = self._runners.get(cfg.id)
        current = runner.current if runner is not None else None
        return {
            "id": cfg.id,
            "name": cfg.name,
            "builtin": cfg.id == BUILTIN_ID,
            "enabled": cfg.enabled,
            "auto": cfg.auto,
            "selected": [] if cfg.auto else list(cfg.capabilities),
            "capabilities": sorted(self.effective_capabilities(cfg, ceiling)),
            "online": runner is not None,
            "busy": current is not None,
            "current_task": current,
            "created_at": cfg.created_at,
        }

    def list_nodes(self) -> list[dict[str, Any]]:
        with self._lock:
            ceiling = self._ceiling()
            return [self.snapshot(cfg, ceiling) for cfg in self._configs.values()]

    def get_node(self, node_id: str) -> dict[str, Any]:
        with self._lock:
            return self.snapshot(self._require(node_id))

    def _require(self, node_id: str) -> LocalNodeConfig:
        cfg = self._configs.get(node_id)
        if cfg is None:
            raise KeyError(node_id)
        return cfg

    # ------------------------------------------------------------ 修改
    @staticmethod
    def _clean_name(name: str) -> str:
        return name.strip()[:MAX_NAME_LENGTH]

    def add_node(
        self,
        name: str = "",
        *,
        auto: bool = False,
        capabilities: list[str] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        with self._lock:
            if len(self._configs) >= MAX_LOCAL_NODES:
                raise ValueError(f"最多创建 {MAX_LOCAL_NODES} 个本机节点")
            selected = [] if auto else self._validate_capabilities(capabilities or [])
            title = self._clean_name(name) or f"本机节点 {len(self._configs)}"
            if any(item.name == title for item in self._configs.values()):
                raise ValueError(f"已经有名为「{title}」的本机节点，请换一个名称")
            cfg = LocalNodeConfig(
                id=f"local-{uuid.uuid4().hex[:12]}", name=title, auto=auto, capabilities=selected, enabled=enabled
            )
            self._configs[cfg.id] = cfg
            self._save()
            self._launch(cfg)
            return self.snapshot(cfg)

    def update_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        auto: bool | None = None,
        capabilities: list[str] | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            cfg = self._require(node_id)
            before = (cfg.enabled, cfg.auto, tuple(cfg.capabilities))
            if name is not None:
                title = self._clean_name(name)
                if not title:
                    raise ValueError("节点名称不能为空")
                if any(item.id != node_id and item.name == title for item in self._configs.values()):
                    raise ValueError(f"已经有名为「{title}」的本机节点，请换一个名称")
                cfg.name = title
            if auto is not None:
                cfg.auto = auto
            if capabilities is not None and not cfg.auto:
                cfg.capabilities = self._validate_capabilities(capabilities)
            if not cfg.auto and not cfg.capabilities:
                raise ValueError("请至少选择一项能力，或改为自动跟随本机检测结果")
            if enabled is not None:
                cfg.enabled = enabled
            self._save()

            runner = self._runners.get(node_id)
            if (cfg.enabled, cfg.auto, tuple(cfg.capabilities)) == before and runner is not None:
                # 只改了名称：重新登记即可，不打断正在执行的阶段
                runner.name = cfg.name
                runner.register()
            else:
                self._halt(node_id)
                self._launch(cfg)
            return self.snapshot(cfg)

    def delete_node(self, node_id: str, delete_record: Callable[[str], bool]) -> None:
        """删除本机节点。``delete_record`` 负责删除队列里的节点记录，节点仍有未完成任务时会抛 ValueError。"""
        with self._lock:
            cfg = self._require(node_id)
            if node_id == BUILTIN_ID:
                raise ValueError("内置节点不能删除，可以在编辑里停用它")
            self._halt(node_id)
            try:
                delete_record(node_id)
            except ValueError:
                self._launch(cfg)  # 删除失败时恢复运行，避免留下一个静默停摆的节点
                raise
            del self._configs[node_id]
            self._save()


__all__ = ["BUILTIN_ID", "LocalNodeConfig", "LocalNodeManager"]

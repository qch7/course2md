"""本机计算节点的增删改。"""

from __future__ import annotations

from typing import Any

from ...core.accounts import LOCAL_USER, User
from ...jobs.manager import JobManager
from .common import Forbidden, NotFound
from .workers import require_queue


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
    queue = require_queue(manager)
    try:
        nodes.delete_node(node_id, lambda worker_id: queue.delete_worker(worker_id, allow_builtin=True))
    except KeyError as exc:
        raise NotFound("本机节点不存在") from exc
    return {"ok": True}

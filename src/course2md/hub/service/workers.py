"""计算节点：令牌与停用、删除。"""

from __future__ import annotations

from ...core.accounts import LOCAL_USER, User
from ...jobs.manager import JobManager
from ..access import NotFound


def list_workers(manager: JobManager, user: User = LOCAL_USER) -> list[dict]:
    """共享节点（无归属）对所有人可见，个人节点只对主人可见，管理员看到全部。"""
    queue = getattr(manager, "queue", None)
    workers = queue.workers() if queue is not None else []
    return [item for item in workers if user.is_admin or item["owner_id"] in (None, user.id)]


def require_queue(manager: JobManager):
    queue = getattr(manager, "queue", None)
    if queue is None:
        raise ValueError("Hub 未启用任务队列")
    return queue


def create_worker_token(manager: JobManager, body: dict, user: User = LOCAL_USER) -> dict:
    """管理员默认创建共享节点（可领取所有人的任务），普通用户创建只处理自己任务的个人节点。"""
    name = str(body.get("name") or "").strip()[:100] or "计算节点"
    shared = user.is_admin and not body.get("personal")
    owner = None if shared else user.id
    worker_id, token = require_queue(manager).create_worker_token(name, owner_id=owner)
    return {"worker_id": worker_id, "name": name, "token": token, "shared": shared}


def disable_worker(manager: JobManager, worker_id: str, user: User = LOCAL_USER) -> dict:
    queue = require_queue(manager)
    owner = next((item["owner_id"] for item in queue.workers() if item["id"] == worker_id), "missing")
    if owner == "missing" or not (user.is_admin or owner == user.id):
        raise NotFound("节点不存在或不能停用")
    if not queue.disable_worker(worker_id):
        raise NotFound("节点不存在或不能停用")
    return {"ok": True}


def delete_worker(manager: JobManager, worker_id: str, user: User = LOCAL_USER) -> dict:
    queue = require_queue(manager)
    owner = next((item["owner_id"] for item in queue.workers() if item["id"] == worker_id), "missing")
    if owner == "missing" or not (user.is_admin or owner == user.id):
        raise NotFound("节点不存在或不能删除")
    if not queue.delete_worker(worker_id):
        raise NotFound("节点不存在或不能删除")
    return {"ok": True}

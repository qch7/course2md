"""本地应用对外连接的 Hub 链接管理。"""

from __future__ import annotations

import platform
from typing import Any

from ...core.accounts import LOCAL_USER, User
from ...jobs.manager import JobManager
from ..access import Forbidden, NotFound, server_mode


def _require_hub_links(manager: JobManager, user: User):
    if not user.is_admin:
        raise Forbidden("只有管理员可以管理对外连接的 Hub")
    links = getattr(manager, "hub_links", None)
    if links is None:
        raise NotFound("当前实例未启用对外节点连接")
    return links


def list_hub_links(manager: JobManager, user: User = LOCAL_USER) -> dict[str, Any]:
    links = getattr(manager, "hub_links", None)
    if not user.is_admin or links is None:
        return {
            "links": [],
            "can_manage": False,
            "capabilities": [],
            "default_name": "",
            "default_work_dir": "",
        }
    return {
        "links": links.list_links(),
        "can_manage": True,
        "capabilities": links.local_capabilities(),
        "default_name": platform.node() or "计算节点",
        "default_work_dir": str(links.default_work_dir),
    }


def create_hub_link(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    without = body.get("without")
    if isinstance(without, str):
        without = [item.strip() for item in without.split(",") if item.strip()]
    link = links.add_link(
        str(body.get("hub_url", "")),
        str(body.get("token", "")),
        name=str(body.get("name", "")),
        work_dir=str(body.get("work_dir", "")),
        auto_connect=bool(body.get("auto_connect", True)),
        no_agents=bool(body.get("no_agents", False)),
        without=list(without) if isinstance(without, (list, tuple)) else None,
        connect_now=bool(body.get("connect_now", True)),
        allow_custom_work_dir=not server_mode(manager),
    )
    return {"link": link}


def update_hub_link(manager: JobManager, link_id: str, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    without = body.get("without") if "without" in body else None
    if isinstance(without, str):
        without = [item.strip() for item in without.split(",") if item.strip()]
    try:
        link = links.update_link(
            link_id,
            hub_url=str(body["hub_url"]) if "hub_url" in body and body["hub_url"] is not None else None,
            token=str(body["token"]) if "token" in body and body["token"] is not None else None,
            name=str(body["name"]) if "name" in body and body["name"] is not None else None,
            work_dir=str(body["work_dir"]) if "work_dir" in body and body["work_dir"] is not None else None,
            auto_connect=bool(body["auto_connect"])
            if "auto_connect" in body and body["auto_connect"] is not None
            else None,
            no_agents=bool(body["no_agents"]) if "no_agents" in body and body["no_agents"] is not None else None,
            without=list(without) if isinstance(without, (list, tuple)) else None,
            allow_custom_work_dir=not server_mode(manager),
        )
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc
    return {"link": link}


def connect_hub_link(manager: JobManager, link_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    try:
        return {"link": links.connect(link_id)}
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc


def disconnect_hub_link(manager: JobManager, link_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    try:
        return {"link": links.disconnect(link_id)}
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc


def delete_hub_link(manager: JobManager, link_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    links = _require_hub_links(manager, user)
    try:
        links.delete_link(link_id)
    except KeyError as exc:
        raise NotFound("外部 Hub 连接不存在") from exc
    return {"ok": True}

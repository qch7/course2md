"""计算服务：节点令牌、对外 Hub 链接与本机节点。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from ... import responses as r
from ... import service
from ...schemas import (
    HubLinkCreateRequest,
    HubLinkUpdateRequest,
    LocalNodeCreateRequest,
    LocalNodeUpdateRequest,
    WorkerTokenRequest,
)
from .deps import CurrentUser, get_manager

router = APIRouter()


@router.post("/workers", tags=["计算服务"], summary="创建节点令牌；令牌只在本次返回", response_model=r.WorkerToken)
def create_worker(body: WorkerTokenRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_worker_token(get_manager(request), body.payload(), user)


@router.delete("/workers/{worker_id}", tags=["计算服务"], summary="停用或永久删除节点并作废其令牌", response_model=r.Ok)
def delete_worker(
    worker_id: str,
    request: Request,
    user: CurrentUser,
    permanent: bool = Query(False, description="永久删除节点记录；默认仅停用。内置节点和仍有未完成任务的节点不能删除"),
) -> dict[str, Any]:
    if permanent:
        return service.delete_worker(get_manager(request), worker_id, user)
    return service.disable_worker(get_manager(request), worker_id, user)


@router.get("/workers", tags=["计算服务"], summary="计算节点、在线状态与能力", response_model=r.WorkersResponse)
def list_workers(request: Request, user: CurrentUser) -> dict[str, Any]:
    return {"workers": service.list_workers(get_manager(request), user)}


@router.get("/hub-links", tags=["计算服务"], summary="本机对外连接的远程 Hub 列表", response_model=r.HubLinksResponse)
def list_hub_links(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_hub_links(get_manager(request), user)


@router.post("/hub-links", tags=["计算服务"], summary="添加对外连接的远程 Hub", response_model=r.HubLinkResponse)
def create_hub_link(body: HubLinkCreateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_hub_link(get_manager(request), body.payload(), user)


@router.patch(
    "/hub-links/{link_id}", tags=["计算服务"], summary="修改对外连接的远程 Hub 配置", response_model=r.HubLinkResponse
)
def update_hub_link(link_id: str, body: HubLinkUpdateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.update_hub_link(get_manager(request), link_id, body.payload(), user)


@router.post(
    "/hub-links/{link_id}/connect", tags=["计算服务"], summary="连接指定的远程 Hub", response_model=r.HubLinkResponse
)
def connect_hub_link(link_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.connect_hub_link(get_manager(request), link_id, user)


@router.post(
    "/hub-links/{link_id}/disconnect",
    tags=["计算服务"],
    summary="断开指定的远程 Hub",
    response_model=r.HubLinkResponse,
)
def disconnect_hub_link(link_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.disconnect_hub_link(get_manager(request), link_id, user)


@router.delete("/hub-links/{link_id}", tags=["计算服务"], summary="删除对外连接的远程 Hub", response_model=r.Ok)
def delete_hub_link(link_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_hub_link(get_manager(request), link_id, user)


@router.get("/local-nodes", tags=["计算服务"], summary="本机节点及其能力", response_model=r.LocalNodesResponse)
def list_local_nodes(request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.list_local_nodes(get_manager(request), user)


@router.post("/local-nodes", tags=["计算服务"], summary="创建本机节点", response_model=r.LocalNodeResponse)
def create_local_node(body: LocalNodeCreateRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.create_local_node(get_manager(request), body.payload(), user)


@router.patch(
    "/local-nodes/{node_id}",
    tags=["计算服务"],
    summary="修改本机节点名称、能力或启停",
    response_model=r.LocalNodeResponse,
)
def update_local_node(
    node_id: str, body: LocalNodeUpdateRequest, request: Request, user: CurrentUser
) -> dict[str, Any]:
    return service.update_local_node(get_manager(request), node_id, body.payload(), user)


@router.delete("/local-nodes/{node_id}", tags=["计算服务"], summary="删除本机节点（内置节点除外）", response_model=r.Ok)
def delete_local_node(node_id: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return service.delete_local_node(get_manager(request), node_id, user)

"""配置与来源解析插件的接口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query, Request

from ... import plugins, service
from ... import responses as r
from ...schemas import (
    PluginInstallRequest,
)
from .deps import CurrentUser, get_manager

router = APIRouter()


@router.get("/plugins", tags=["插件"], summary="已安装的来源解析插件与管理状态", response_model=r.PluginsResponse)
def list_plugins(request: Request, user: CurrentUser) -> dict[str, Any]:
    return plugins.list_plugins(get_manager(request), user)


@router.post(
    "/plugins", tags=["插件"], summary="从 GitHub 或本机路径安装插件（本地应用）", response_model=r.PluginsResponse
)
def install_plugin(body: PluginInstallRequest, request: Request, user: CurrentUser) -> dict[str, Any]:
    return plugins.install_plugin(get_manager(request), body.source, body.kind, user)


@router.post(
    "/plugins/import", tags=["插件"], summary="上传插件安装包（本地应用，最大 32 MB）", response_model=r.PluginsResponse
)
def import_plugin(
    request: Request,
    user: CurrentUser,
    filename: str = Query(..., description=".whl、.zip 或 .tar.gz 文件名"),
    content: bytes = Body(..., media_type="application/octet-stream"),
) -> dict[str, Any]:
    return plugins.import_plugin(get_manager(request), filename, content, user)


@router.delete(
    "/plugins/{name}", tags=["插件"], summary="卸载来源解析插件（本地应用）", response_model=r.PluginsResponse
)
def delete_plugin(name: str, request: Request, user: CurrentUser) -> dict[str, Any]:
    return plugins.delete_plugin(get_manager(request), name, user)


@router.get("/config", tags=["配置"], summary="本机配置、默认值与可选项", response_model=r.Config)
def get_config(request: Request, user: CurrentUser) -> dict[str, Any]:
    payload = service.config_payload(get_manager(request), user)
    if service.server_mode(get_manager(request)):
        # 服务器路径与模型位置只对管理员显示
        payload["server"] = True
        if not user.is_admin:
            payload.pop("default_out", None)
            payload.pop("model_path", None)
    return payload

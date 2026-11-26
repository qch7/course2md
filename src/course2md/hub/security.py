"""本地模式的请求来源检查（ASGI 中间件）。

- 防 DNS 重绑定：Host 必须是回环地址、监听地址或 COURSE2MD_ALLOWED_HOSTS 中的名字，端口与实际连接端口一致。
- 防跨站写入：写操作只接受同源页面或不带 Origin 的本机客户端，并要求 JSON 请求体。
- 请求体大小上限。

服务器形态启用账号鉴权后，这些检查由正式鉴权与跨域策略取代（见阶段 5）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
MAX_BODY_BYTES = 1024 * 1024
MAX_PLUGIN_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
PLUGIN_UPLOAD_PATH = "/api/v1/plugins/import"
BUNDLE_UPLOAD_PATH = "/api/v1/bundles/import"
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


def _headers(scope: Scope) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}


def allowed_host(host_header: str, bind_host: str, port: int | None) -> bool:
    address = urlparse(f"http://{host_header.strip().lower()}")
    try:
        name, requested_port = address.hostname or "", address.port
    except ValueError:
        return False
    if ":" in name:
        name = f"[{name}]"
    allowed = {
        *LOOPBACK_HOSTS,
        *(item.strip().lower() for item in os.environ.get("COURSE2MD_ALLOWED_HOSTS", "").split(",") if item.strip()),
    }
    bound = bind_host.lower()
    if bound not in ("0.0.0.0", "::", ""):
        allowed.add(f"[{bound}]" if ":" in bound else bound)
    return name in allowed and requested_port in (None, port)


def allowed_write(
    headers: dict[str, str],
    method: str = "POST",
    plugin_upload: bool = False,
    bundle_upload: bool = False,
) -> bool:
    origin = headers.get("origin")
    if origin is not None:
        address = urlparse(origin)
        if address.scheme not in ("http", "https") or address.netloc.lower() != headers.get("host", "").strip().lower():
            return False
    if method == "DELETE":  # 跨站 DELETE 必须先经过 CORS 预检，只需检查来源
        return True
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if plugin_upload:
        return content_type == "application/octet-stream"
    if bundle_upload:
        return content_type in ("application/octet-stream", "application/zip")
    return content_type == "application/json"


class LocalOriginGuard:
    """bind_host 为服务监听的地址；端口取自实际连接（scope["server"]）。"""

    def __init__(
        self, app, bind_host: str = "127.0.0.1", exempt_prefixes: tuple[str, ...] = (), check_host: bool = True
    ) -> None:
        self.app = app
        self.bind_host = bind_host
        self.exempt_prefixes = exempt_prefixes
        self.check_host = check_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        server = scope.get("server") or (None, None)
        if self.check_host and not allowed_host(headers.get("host", ""), self.bind_host, server[1]):
            await _reject(send, 403, "请求的主机名不受信任")
            return
        if scope["method"] in WRITE_METHODS:
            plugin_upload = scope["path"] == PLUGIN_UPLOAD_PATH and scope["method"] == "POST"
            bundle_upload = scope["path"] == BUNDLE_UPLOAD_PATH and scope["method"] == "POST"
            if not allowed_write(headers, scope["method"], plugin_upload, bundle_upload):
                await _reject(send, 403, "已拒绝跨站请求，请在 course2md 页面中操作")
                return
            length = headers.get("content-length", "0").strip() or "0"
            if not length.isdecimal():
                await _reject(send, 400, "请求长度无效")
                return
            maximum = MAX_BUNDLE_BYTES if bundle_upload else (MAX_PLUGIN_BYTES if plugin_upload else MAX_BODY_BYTES)
            if int(length) > maximum:
                await _reject(send, 400, "请求内容过大")
                return
            received = 0

            async def limited_receive() -> dict[str, Any]:
                nonlocal received
                message = await receive()
                received += len(message.get("body", b""))
                if received > maximum:
                    raise ValueError("请求内容过大")
                return message

            await self.app(scope, limited_receive, send)
            return
        await self.app(scope, receive, send)


async def _reject(send: Send, status: int, message: str) -> None:
    body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

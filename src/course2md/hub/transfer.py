"""Hub 间课程产物互传：对端 Hub 配置管理、安全校验与 HTTP 传输客户端。"""

from __future__ import annotations

import ipaddress
import os
import socket
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests

from ..config import env
from ..config.schema import TRUE_WORDS
from ..core.crypto import FieldCipher
from ..errors import Course2mdError
from ..jobs.bundle import CONFLICT_MODES, MAX_BUNDLE_BYTES
from ..jobs.manager import ACTIVE_STATUSES
from ..resolvers.base import TLS_HINT, tls_verify
from ..util.jsonfile import read_json_object, write_json_private
from ..worker.link import normalize_hub_url, token_hint

_CONNECT_TIMEOUT = 10.0
_CATALOG_TIMEOUT = 20.0
_TRANSFER_TIMEOUT = 180.0
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class TransferError(Course2mdError):
    """Hub 间传输失败（网络、鉴权、协议或安全边界错误）。"""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _origin_key(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port


def _check_ip(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    allow_private: bool,
) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if address.is_link_local or address.is_unspecified or address.is_multicast:
        raise TransferError(f"出于安全限制，不允许向该地址发起互传（{address}）", status_code=400)
    if not allow_private and (address.is_loopback or address.is_private or address.is_reserved):
        raise TransferError(
            f"服务器形态下普通用户不能向内网或本机地址发起互传（{address}）；"
            "如需允许，请联系管理员开启 COURSE2MD_ALLOW_PRIVATE_TRANSFER=1",
            status_code=403,
        )


def validate_remote_hub_url(  # noqa: PLR0912  历史遗留的长函数，拆分后移除
    raw_url: str,
    *,
    server_mode: bool = False,
    is_admin: bool = True,
    local_origins: set[str] | None = None,
) -> str:
    """规范化对端 Hub 地址，并按部署形态执行自环与 SSRF 检查。"""
    try:
        hub_url = normalize_hub_url(raw_url)
    except ValueError as exc:
        raise TransferError(str(exc), status_code=400) from None

    if local_origins:
        target_origin = _origin_key(hub_url)
        for local in local_origins:
            try:
                if _origin_key(normalize_hub_url(local)) == target_origin:
                    raise TransferError("不能以当前 Hub 自身作为对端传输目标", status_code=400)
            except ValueError:
                continue

    if server_mode:
        parsed = urlparse(hub_url)
        host = (parsed.hostname or "").strip()
        allow_private = is_admin or env("COURSE2MD_ALLOW_PRIVATE_TRANSFER").strip().lower() in TRUE_WORDS
        try:
            literal_ip = ipaddress.ip_address(host)
        except ValueError:
            try:
                infos = socket.getaddrinfo(
                    host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
                )
            except OSError as exc:
                raise TransferError(f"无法解析对端 Hub 地址（{host}）：{exc}", status_code=400) from None
            if not infos:
                raise TransferError(f"无法解析对端 Hub 地址（{host}）", status_code=400) from None
            for info in infos:
                sockaddr = info[4]
                if not sockaddr:
                    continue
                try:
                    resolved = ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0])
                except ValueError:
                    continue
                _check_ip(resolved, allow_private=allow_private)
        else:
            _check_ip(literal_ip, allow_private=allow_private)

    return hub_url


@dataclass
class TransferPeer:
    """用户保存的对端 Hub 信息（令牌加密存储）。"""

    id: str
    owner: str
    hub_url: str
    token: str = ""
    name: str = ""
    created_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        parsed = urlparse(self.hub_url)
        default_name = parsed.netloc or self.hub_url
        return {
            "id": self.id,
            "name": self.name or default_name,
            "hub_url": self.hub_url,
            "has_token": bool(self.token.strip()),
            "token_hint": token_hint(self.token),
            "created_at": self.created_at,
        }


class TransferPeerStore:
    """持久化用户保存的对端 Hub 列表，按用户隔离并加密令牌。"""

    def __init__(self, state_path: Path, cipher: FieldCipher | None = None) -> None:
        self.state_path = state_path
        self.cipher = cipher or FieldCipher.from_env()
        self._lock = threading.RLock()
        self._peers: dict[str, TransferPeer] = {}
        self._load()

    def _load(self) -> None:
        raw = read_json_object(self.state_path)
        if raw is None:
            return
        for entry in raw.get("peers") or []:
            if not isinstance(entry, dict):
                continue
            raw_url = str(entry.get("hub_url") or "").strip()
            if not raw_url:
                continue
            try:
                hub_url = normalize_hub_url(raw_url)
            except ValueError:
                continue
            raw_token = str(entry.get("token") or "")
            try:
                token = self.cipher.decrypt(raw_token) if raw_token else ""
            except Exception:  # noqa: BLE001  密钥变更时保留条目但清空不可解密的令牌，避免阻断启动
                token = ""
            peer = TransferPeer(
                id=str(entry.get("id") or f"peer-{uuid.uuid4().hex[:12]}"),
                owner=str(entry.get("owner") or "local"),
                hub_url=hub_url,
                token=token,
                name=str(entry.get("name") or ""),
                created_at=float(entry.get("created_at") or time.time()),
            )
            self._peers[peer.id] = peer

    def _save(self) -> None:
        items = [
            {
                "id": peer.id,
                "owner": peer.owner,
                "hub_url": peer.hub_url,
                "token": self.cipher.encrypt(peer.token) if peer.token else "",
                "name": peer.name,
                "created_at": peer.created_at,
            }
            for peer in self._peers.values()
        ]
        write_json_private(self.state_path, {"version": 1, "peers": items}, prefix=".hub-peers-")

    def list_for(self, owner: str) -> list[dict[str, Any]]:
        with self._lock:
            peers = [peer for peer in self._peers.values() if peer.owner == owner]
            peers.sort(key=lambda item: item.created_at)
            return [peer.public() for peer in peers]

    def get_for(self, owner: str, peer_id: str) -> TransferPeer | None:
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None or peer.owner != owner:
                return None
            return peer

    def add(self, owner: str, hub_url: str, token: str = "", name: str = "") -> dict[str, Any]:
        normalized = normalize_hub_url(hub_url)
        clean_name = name.strip()[:100]
        clean_token = token.strip()
        with self._lock:
            peer = TransferPeer(
                id=f"peer-{uuid.uuid4().hex[:12]}",
                owner=owner,
                hub_url=normalized,
                token=clean_token,
                name=clean_name,
            )
            self._peers[peer.id] = peer
            self._save()
            return peer.public()

    def update(
        self,
        owner: str,
        peer_id: str,
        *,
        hub_url: str | None = None,
        token: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None or peer.owner != owner:
                raise KeyError(peer_id)
            if hub_url is not None:
                peer.hub_url = normalize_hub_url(hub_url)
            if token is not None:
                peer.token = token.strip()
            if name is not None:
                peer.name = name.strip()[:100]
            self._save()
            return peer.public()

    def delete(self, owner: str, peer_id: str) -> None:
        with self._lock:
            peer = self._peers.get(peer_id)
            if peer is None or peer.owner != owner:
                raise KeyError(peer_id)
            self._peers.pop(peer_id, None)
            self._save()


def _error_detail(response: requests.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            return str(data.get("error") or data.get("detail") or "").strip()
    except ValueError:
        pass
    return (response.text or "").strip()[:300]


class RemoteHubClient:
    """与对端 Hub 通信的 HTTP 客户端。"""

    def __init__(
        self,
        hub_url: str,
        token: str = "",
        *,
        session: requests.Session | None = None,
        verify: bool | str | None = None,
    ) -> None:
        self.hub_url = normalize_hub_url(hub_url)
        self.token = token.strip()
        self.session = session or requests.Session()
        self._owns_session = session is None
        self.verify = tls_verify() if verify is None else verify

    def close(self) -> None:
        if self._owns_session:
            self.session.close()

    def __enter__(self) -> RemoteHubClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: tuple[float, float],
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: Any = None,
        stream: bool = False,
    ) -> requests.Response:
        url = f"{self.hub_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers=self._headers(headers),
                params=params,
                data=data,
                timeout=timeout,
                verify=self.verify,
                stream=stream,
                allow_redirects=False,
            )
        except requests.exceptions.SSLError as exc:
            raise TransferError(f"对端 Hub 的 {TLS_HINT}（{exc}）", status_code=502) from None
        except requests.exceptions.Timeout:
            raise TransferError(f"连接对端 Hub 超时（{self.hub_url}），请检查网络或服务状态", status_code=502) from None
        except requests.RequestException as exc:
            raise TransferError(f"无法连接对端 Hub（{self.hub_url}）：{exc}", status_code=502) from None

        if response.status_code in _REDIRECT_CODES:
            location = response.headers.get("location", "")
            response.close()
            hint = f"（重定向目标：{location}）" if location else ""
            raise TransferError(f"对端 Hub 返回了重定向{hint}，请直接填写最终的 http(s) 地址", status_code=400)

        if response.status_code == 401:
            detail = _error_detail(response)
            response.close()
            raise TransferError(
                detail or "对端 Hub 鉴权失败，请填写有效的 API 令牌（在对端「账号」页生成）",
                status_code=401,
            )
        if response.status_code == 403:
            detail = _error_detail(response)
            response.close()
            raise TransferError(
                f"对端 Hub 拒绝了请求：{detail or '请检查权限或对端 COURSE2MD_ALLOWED_HOSTS 配置'}",
                status_code=403,
            )
        if response.status_code == 404:
            detail = _error_detail(response)
            response.close()
            raise TransferError(
                detail or "对端 Hub 未找到对应资源，或对端版本过旧尚未支持课程互传接口",
                status_code=404,
            )
        if response.status_code >= 400:
            detail = _error_detail(response)
            response.close()
            raise TransferError(
                detail or f"对端 Hub 返回错误（HTTP {response.status_code}）",
                status_code=400 if response.status_code < 500 else 502,
            )
        return response

    def catalog(self) -> dict[str, Any]:
        """读取对端 Hub 的模式与可传输课程/课次目录。"""
        status_resp = self._request("GET", "/api/v1/auth/status", timeout=(_CONNECT_TIMEOUT, _CATALOG_TIMEOUT))
        try:
            auth_status = status_resp.json()
        except ValueError as exc:
            raise TransferError("对端地址不是有效的 course2md Hub 接口", status_code=502) from exc
        mode = str(auth_status.get("mode") or "local")

        courses_resp = self._request("GET", "/api/v1/courses", timeout=(_CONNECT_TIMEOUT, _CATALOG_TIMEOUT))
        jobs_resp = self._request("GET", "/api/v1/jobs", timeout=(_CONNECT_TIMEOUT, _CATALOG_TIMEOUT))
        try:
            raw_courses = list((courses_resp.json() or {}).get("courses") or [])
            raw_jobs = list((jobs_resp.json() or {}).get("jobs") or [])
        except ValueError as exc:
            raise TransferError("对端 Hub 返回的数据格式异常", status_code=502) from exc

        groups: dict[str, dict[str, Any]] = {}
        for item in raw_courses:
            cid = str(item.get("id") or "")
            if not cid:
                continue
            groups[cid] = {
                "id": cid,
                "title": str(item.get("title") or item.get("course") or "课程"),
                "course": str(item.get("course") or item.get("title") or "课程"),
                "source": str(item.get("source") or ""),
                "updated_at": float(item.get("updated_at") or 0.0),
                "jobs": [],
            }

        for job in raw_jobs:
            jid = str(job.get("id") or "")
            if not jid:
                continue
            cid = str(job.get("course_id") or "")
            key = cid if cid in groups else f"standalone:{job.get('course') or '未分类'}"
            if key not in groups:
                groups[key] = {
                    "id": "",
                    "title": str(job.get("course") or "未分类"),
                    "course": str(job.get("course") or "未分类"),
                    "source": "",
                    "updated_at": float(job.get("started_at") or 0.0),
                    "jobs": [],
                }
            status = str(job.get("status") or "")
            files = job.get("files") or []
            transferable = status not in ACTIVE_STATUSES and not job.get("pending_messages") and len(files) > 0
            groups[key]["jobs"].append(
                {
                    "id": jid,
                    "title": str(job.get("title") or ""),
                    "course": str(job.get("course") or ""),
                    "course_id": cid,
                    "session_id": str(job.get("session_id") or ""),
                    "session_date": str(job.get("session_date") or ""),
                    "week": job.get("week"),
                    "status": status,
                    "agent": str(job.get("agent") or ""),
                    "agent_model": str(job.get("agent_model") or ""),
                    "started_at": float(job.get("started_at") or 0.0),
                    "file_count": len(files),
                    "transferable": transferable,
                }
            )

        ordered = sorted(groups.values(), key=lambda item: item["updated_at"], reverse=True)
        for group in ordered:
            group["jobs"].sort(key=lambda item: item["started_at"], reverse=True)
        return {
            "hub_url": self.hub_url,
            "mode": mode,
            "courses": [group for group in ordered if group["jobs"]],
        }

    def push_bundle(self, bundle_path: Path, *, conflict: str = "skip") -> dict[str, Any]:
        """将本地打包好的 .c2m.zip 推送导入到对端 Hub。"""
        if conflict not in CONFLICT_MODES:
            raise TransferError(f"不支持的冲突策略：{conflict!r}", status_code=400)
        size = bundle_path.stat().st_size
        with bundle_path.open("rb") as handle:
            response = self._request(
                "POST",
                "/api/v1/bundles/import",
                params={"conflict": conflict},
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                },
                data=handle,
                timeout=(_CONNECT_TIMEOUT, _TRANSFER_TIMEOUT),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransferError("对端 Hub 返回的导入结果不是有效的 JSON", status_code=502) from exc
        if not isinstance(payload, dict):
            raise TransferError("对端 Hub 返回的导入结果格式异常", status_code=502)
        return payload

    def _download_stream(self, path: str, target_path: Path, *, params: dict[str, Any] | None = None) -> Path:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target_path.parent, prefix=".c2m-dl-", suffix=".zip")
        fd_open = True
        total = 0
        try:
            with self._request(
                "GET", path, params=params, timeout=(_CONNECT_TIMEOUT, _TRANSFER_TIMEOUT), stream=True
            ) as response:
                content_length = response.headers.get("content-length", "").strip()
                if content_length.isdecimal() and int(content_length) > MAX_BUNDLE_BYTES:
                    raise TransferError(
                        f"对端产物包超过大小上限（{MAX_BUNDLE_BYTES // (1024 * 1024)} MB）",
                        status_code=400,
                    )
                fd_open = False
                with os.fdopen(fd, "wb") as output:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_BUNDLE_BYTES:
                            raise TransferError(
                                f"对端产物包超过大小上限（{MAX_BUNDLE_BYTES // (1024 * 1024)} MB）",
                                status_code=400,
                            )
                        output.write(chunk)
            if total == 0:
                raise TransferError("对端 Hub 返回了空的产物包", status_code=502)
            os.replace(tmp_name, target_path)
            return target_path
        except BaseException:
            if fd_open:
                os.close(fd)
            if Path(tmp_name).exists():
                Path(tmp_name).unlink(missing_ok=True)
            raise

    def download_course_bundle(
        self,
        course_id: str,
        target_path: Path,
        *,
        job_ids: list[str] | None = None,
        include_cache: bool = False,
    ) -> Path:
        params: dict[str, Any] = {"job_ids": ",".join(job_ids)} if job_ids else {}
        if include_cache:
            params["include_cache"] = "true"
        return self._download_stream(
            f"/api/v1/courses/{quote(course_id, safe='')}/bundle",
            target_path,
            params=params or None,
        )

    def download_job_bundle(self, job_id: str, target_path: Path, *, include_cache: bool = False) -> Path:
        return self._download_stream(
            f"/api/v1/jobs/{quote(job_id, safe='')}/bundle",
            target_path,
            params={"include_cache": "true"} if include_cache else None,
        )


__all__ = [
    "RemoteHubClient",
    "TransferError",
    "TransferPeer",
    "TransferPeerStore",
    "validate_remote_hub_url",
]

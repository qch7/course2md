"""来源解析器的数据结构、基类与下载工具。

解析器负责把「用户给的链接或路径」变成可以喂给转写阶段的媒体文件。
第三方解析器应只从 :mod:`course2md.resolvers.api` 导入，详见 docs/plugins.md。
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

from ..errors import Course2mdError

# 解析器接口的主版本号。不兼容的改动才会递增，插件声明的 api_version 必须与之相同。
RESOLVER_API_VERSION = 1


class ResolverError(Course2mdError):
    """解析失败（链接过期、权限不足、站点改版等），消息会直接展示给用户。"""


INSECURE_TLS_ENV = "COURSE2MD_INSECURE_TLS"
TLS_HINT = (
    f"HTTPS 证书校验失败。确认网络可信后，可在 .env 设置 {INSECURE_TLS_ENV}=1 临时跳过校验，"
    "或用 REQUESTS_CA_BUNDLE 指定学校的根证书"
)


def tls_verify() -> bool:
    """默认校验 HTTPS 证书，仅在用户显式设置时跳过。"""
    return os.environ.get(INSECURE_TLS_ENV, "").strip().lower() not in ("1", "true", "yes")


@dataclass
class MediaRef:
    """一个待处理的媒体：要么是远程直链，要么是本地文件。

    url 以 http(s):// 开头时由核心下载（可在 meta["referer"] 指定来源页），否则视为本地路径。
    name 是下载后的文件名；view 是机位编号；primary 标记笔记使用的主音轨，
    都未标记时依次取机位 1、第一路有讲话的媒体。meta 中的 subject、begin 会写进转写元信息。
    """

    url: str
    name: str
    view: int | None = None
    meta: dict[str, str] = field(default_factory=dict)
    primary: bool = False

    @property
    def is_remote(self) -> bool:
        return self.url.startswith(("http://", "https://"))


@dataclass
class SessionInfo:
    """一节录播（同一课次可能有多个机位）。

    ``week`` 在列出课次后按 :mod:`course2md.resolvers.week` 填入；
    ``course`` 用于界面上按课程分组、以及输出目录的课程文件夹。
    """

    session_id: str
    date: str
    title: str
    views: int = 0
    week: int | None = None
    course: str = ""
    meta: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ViewOption:
    """一路可选机位，界面据此显示勾选项；default 为 True 时默认勾选。"""

    id: int
    label: str
    default: bool = False


@runtime_checkable
class Resolver(Protocol):
    """解析器必须提供的成员，其余可选钩子见 :class:`BaseResolver`。"""

    name: str

    def matches(self, source: str) -> bool:
        """判断该来源是否由本解析器处理。"""
        ...

    def list_sessions(self, source: str) -> list[SessionInfo]:
        """列出可选课次，按时间倒序。"""
        ...

    def resolve(self, source: str, session_id: str | None = None, views: list[int] | None = None) -> list[MediaRef]:
        """解析出媒体。session_id 为空时取最近一节课，views 为空时取全部默认机位。"""
        ...


class BaseResolver:
    """解析器基类，提供可选钩子的默认实现。插件继承它并实现三个必需方法即可。

    类属性：
      name         唯一标识，只用小写字母、数字与 - _，同时作为公开来源描述的前缀
      label        界面显示名称，例如「示例课程平台」
      source_hint  输入框旁的说明，例如「粘贴播放页地址」
      priority     匹配顺序，数值大的先尝试；内置本地文件解析器为 -100
      api_version  声明实现的接口版本，须等于 RESOLVER_API_VERSION
    """

    name: ClassVar[str] = ""
    label: ClassVar[str] = ""
    source_hint: ClassVar[str] = ""
    priority: ClassVar[int] = 0
    api_version: ClassVar[int] = RESOLVER_API_VERSION

    def matches(self, source: str) -> bool:
        raise NotImplementedError

    def list_sessions(self, source: str) -> list[SessionInfo]:
        raise NotImplementedError

    def resolve(self, source: str, session_id: str | None = None, views: list[int] | None = None) -> list[MediaRef]:
        raise NotImplementedError

    def public_source(self, source: str) -> str:
        """可写入日志、job.json 与对话记录的来源描述。来源里带登录凭证时必须改写。

        返回 ``f"{name}:..."`` 形式时，核心会把它交回本解析器的 course_identity 识别课程。
        默认去掉 URL 的查询串与片段。
        """
        if source.startswith(("http://", "https://")):
            return source.split("?", 1)[0].split("#", 1)[0]
        return source

    def course_identity(self, source: str) -> str:
        """同一门课在更换凭证、重新复制链接后仍保持不变的标识，用于合并课程记录。

        需要同时接受原始来源与 public_source 的返回值。默认返回来源本身。
        """
        return source

    def view_options(self, source: str) -> list[ViewOption]:
        """该来源可选的机位；返回空列表时界面不显示机位选项。"""
        return []


def _declared_length(resp: Any) -> int | None:
    """读取 Content-Length；缺失返回 None，格式非法直接报错，避免后面按错误长度校验。"""
    length = resp.headers.get("Content-Length")
    if length is not None and not length.isdecimal():
        raise ResolverError("下载响应的 Content-Length 无效，请重新下载")
    return int(length) if length is not None else None


def _check_resume_range(resp: Any, have: int, response_length: int | None) -> tuple[int | None, int]:
    """校验 206 的 Content-Range 与本地分片衔接，返回（文件总长度或 None，本次响应应收字节数）。"""
    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", resp.headers.get("Content-Range", ""))
    if match is None:
        raise ResolverError("续传响应缺少有效的 Content-Range，已保留原分片")
    start, end = int(match[1]), int(match[2])
    total = int(match[3]) if match[3] != "*" else None
    if (
        start != have
        or end < start
        or (total is not None and end >= total)
        or (response_length is not None and response_length != end - start + 1)
    ):
        raise ResolverError("续传响应的字节区间不匹配，已保留原分片")
    return total, end - start + 1


def _write_chunks(
    resp: Any,
    part: Path,
    have: int,
    total: int | None,
    response_length: int | None,
    progress: Callable[[int, int], None] | None,
) -> tuple[int, int]:
    """把响应体写入分片，返回（分片累计字节数，本次收到字节数）。"""
    received = 0
    with part.open("ab" if have else "wb") as handle:
        for chunk in resp.iter_content(1024 * 512):
            if not chunk:
                continue
            if response_length is not None and received + len(chunk) > response_length:
                raise ResolverError("下载响应超出声明的字节数，已保留有效分片，请重试下载")
            handle.write(chunk)
            have += len(chunk)
            received += len(chunk)
            if progress:
                progress(have, total or have)
    return have, received


def download(media: MediaRef, dest: Path, progress=None, session=None) -> Path:
    """下载到 .part 后提交。200 覆盖旧分片，206 校验区间，失败保留分片。

    progress(已下载字节, 总字节) 可选；session 为 requests.Session，省略时临时创建。
    """
    if session is None:
        import requests

        with requests.Session() as owned:
            return download(media, dest, progress, owned)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    headers = {"User-Agent": "Mozilla/5.0", "Referer": media.meta.get("referer", "")}
    if have:
        headers["Range"] = f"bytes={have}-"

    import requests

    try:
        response = session.get(media.url, headers=headers, stream=True, timeout=120, verify=tls_verify())
    except requests.exceptions.SSLError as exc:
        raise ResolverError(TLS_HINT) from exc
    with response as resp:
        if resp.status_code not in (200, 206):
            raise ResolverError(f"下载失败 HTTP {resp.status_code}")
        response_length = _declared_length(resp)
        if resp.status_code == 206:
            total, response_length = _check_resume_range(resp, have, response_length)
        else:
            have = 0
            total = response_length
        have, received = _write_chunks(resp, part, have, total, response_length, progress)
        if (response_length is not None and received != response_length) or (total is not None and have != total):
            raise ResolverError("下载字节数与响应不符，已保留分片，请重试下载")
        if resp.status_code == 206 and total is None:
            raise ResolverError("续传响应未提供文件总长度，已保留分片，请重新下载")
    part.replace(dest)
    return dest

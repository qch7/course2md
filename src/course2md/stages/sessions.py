"""解析阶段：在计算节点上列出来源的课次。

登录课程平台、访问校内网络的请求必须由能访问该网络的节点发出，而不是 Hub。
Hub 只负责派发任务并接收结果，因此 Hub 即使无法访问课程平台也能使用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..resolvers import get_resolver, public_source
from .artifacts import SessionList
from .base import BaseStage, StageContext

if TYPE_CHECKING:
    from .options import JobOptions


def resolver_requires(source: str) -> list[str]:
    """处理该来源所需的节点能力。本地文件只存在于 Hub 所在电脑，由内置节点处理。"""
    try:
        name = get_resolver(source).name
    except Exception:  # noqa: BLE001  解析器缺失时仍允许排队，执行时报告原因
        return []
    return ["resolver:local", "hub"] if name == "local" else [f"resolver:{name}"]


class SessionsStage(BaseStage[None, SessionList]):
    name: ClassVar[str] = "sessions"
    label: ClassVar[str] = "解析课次"
    # 课次会随平台更新，结果不进入缓存复用
    cache_options: ClassVar[tuple[str, ...] | None] = ("source",)

    @classmethod
    def requires(cls, options: JobOptions, inputs: None = None) -> list[str]:
        return resolver_requires(options.source)

    def run(self, ctx: StageContext, inputs: None = None) -> SessionList:
        source = ctx.options.source
        ctx.progress("解析", 0.1, f"解析来源：{public_source(source)[:80]}", None)
        resolver = get_resolver(source)
        ctx.check()
        sessions = resolver.list_sessions(source)
        ctx.progress("解析", 1.0, f"解析到 {len(sessions)} 个课次", None)
        return SessionList(list(sessions))

"""解析器插件的稳定接口。第三方解析器只应从这里导入。

示例::

    from course2md.resolvers.api import BaseResolver, MediaRef, SessionInfo

    class MySiteResolver(BaseResolver):
        name = "mysite"
        label = "某某课堂"

        def matches(self, source): ...
        def list_sessions(self, source): ...
        def resolve(self, source, session_id=None, views=None): ...

在插件的 pyproject.toml 中登记入口点::

    [project.entry-points."course2md.resolvers"]
    mysite = "course2md_mysite:MySiteResolver"

此模块中的名称在 RESOLVER_API_VERSION 不变时保持兼容。完整说明见 docs/plugins.md。
"""

from __future__ import annotations

from .base import (
    INSECURE_TLS_ENV,
    RESOLVER_API_VERSION,
    TLS_HINT,
    BaseResolver,
    MediaRef,
    Resolver,
    ResolverError,
    SessionInfo,
    ViewOption,
    download,
    tls_verify,
)
from .week import parse_date

__all__ = [
    "INSECURE_TLS_ENV",
    "RESOLVER_API_VERSION",
    "TLS_HINT",
    "BaseResolver",
    "MediaRef",
    "Resolver",
    "ResolverError",
    "SessionInfo",
    "ViewOption",
    "download",
    "parse_date",
    "tls_verify",
]

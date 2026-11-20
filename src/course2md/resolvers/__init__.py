"""来源解析：内置本地文件解析器，其它站点通过插件提供（见 :mod:`course2md.resolvers.api`）。"""

from __future__ import annotations

from .base import RESOLVER_API_VERSION, BaseResolver, MediaRef, ResolverError, SessionInfo, ViewOption, download
from .registry import (
    ResolverInfo,
    available,
    course_identity,
    get_resolver,
    load_errors,
    public_source,
    reload,
    view_options,
)
from .week import monday_of, parse_date, resolve_term_start, week_of

__all__ = [
    "RESOLVER_API_VERSION",
    "BaseResolver",
    "MediaRef",
    "ResolverError",
    "ResolverInfo",
    "SessionInfo",
    "ViewOption",
    "available",
    "course_identity",
    "download",
    "get_resolver",
    "load_errors",
    "monday_of",
    "parse_date",
    "public_source",
    "reload",
    "resolve_term_start",
    "view_options",
    "week_of",
]

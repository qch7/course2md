"""解析器注册表：内置解析器、已安装插件与环境变量指定的模块。

加载顺序：
1. 内置解析器（本地文件 / 目录）；
2. 已安装包在 ``course2md.resolvers`` 入口点组中登记的解析器；
3. ``COURSE2MD_RESOLVERS`` 中以逗号分隔的 ``模块:属性``，便于开发时不安装直接加载。

``COURSE2MD_DISABLED_RESOLVERS`` 可按 name 停用解析器。加载失败的插件不会影响其它解析器，
原因保存在 :func:`load_errors` 中，由 doctor 与网页启动提示展示。
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from ..plugins import discover
from .base import RESOLVER_API_VERSION, BaseResolver, ResolverError, ViewOption

ENTRY_POINT_GROUP = "course2md.resolvers"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
REQUIRED_METHODS = ("matches", "list_sessions", "resolve")
BUILTINS = {"local": "course2md.resolvers.local:LocalResolver"}


@dataclass(frozen=True)
class ResolverInfo:
    """已注册解析器的说明，供网页、doctor 与 CLI 展示。"""

    name: str
    label: str
    source_hint: str
    priority: int
    origin: str  # builtin、包名 或 env
    version: str = ""


@dataclass
class _Entry:
    info: ResolverInfo
    create: Callable[[], Any]


_lock = threading.Lock()
_entries: list[_Entry] | None = None
_errors: list[str] = []


def _candidates() -> list[tuple[str, str, str, Callable[[], object]]]:
    """返回 (来源说明, 包名, 版本, 加载函数)，发现规则见 :mod:`course2md.plugins`。"""
    return [
        (item.origin, item.package, item.version, item.load)
        for item in discover(ENTRY_POINT_GROUP, BUILTINS, "COURSE2MD_RESOLVERS", _errors)
    ]


def _same(target: Any) -> Any:
    return target


def _validate(obj: object) -> list[str]:
    problems = []
    name = getattr(obj, "name", None)
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        problems.append("name 须为 1-32 位小写字母、数字、- 或 _")
    for method in REQUIRED_METHODS:
        if not callable(getattr(obj, method, None)):
            problems.append(f"缺少方法 {method}")
    version = getattr(obj, "api_version", RESOLVER_API_VERSION)
    if version != RESOLVER_API_VERSION:
        problems.append(f"接口版本 {version} 与 course2md 支持的 {RESOLVER_API_VERSION} 不一致，请升级插件或 course2md")
    return problems


def _load() -> list[_Entry]:
    disabled = {item.strip() for item in os.environ.get("COURSE2MD_DISABLED_RESOLVERS", "").split(",") if item.strip()}
    entries: list[_Entry] = []
    names: set[str] = set()
    loaded: set[int] = set()
    for origin, package, version, load in _candidates():
        try:
            target = load()
            if id(target) in loaded:
                continue  # 已安装的插件又在 COURSE2MD_RESOLVERS 中指定，视为同一个
            loaded.add(id(target))
            # 入口点可以是类、无参工厂函数或已创建的对象。类与工厂在每次匹配时新建实例，
            # 避免插件状态跨请求共享；对象原样复用。
            create: Callable[[], Any]
            if isinstance(target, type) or (callable(target) and not hasattr(target, "matches")):
                create = target
            else:
                create = partial(_same, target)
            sample = create()
        except Exception as exc:  # noqa: BLE001  单个插件出错不影响其它解析器
            _errors.append(f"解析器 {origin} 加载失败（{type(exc).__name__}: {exc}）")
            continue
        problems = _validate(sample)
        if problems:
            _errors.append(f"解析器 {origin} 未启用：{'；'.join(problems)}")
            continue
        name = sample.name
        if name in disabled:
            continue
        if name in names:
            _errors.append(f"解析器 {origin} 未启用：name「{name}」已被其它解析器使用")
            continue
        names.add(name)
        info = ResolverInfo(
            name=name,
            label=str(getattr(sample, "label", "") or name),
            source_hint=str(getattr(sample, "source_hint", "") or ""),
            priority=int(getattr(sample, "priority", 0) or 0),
            origin="builtin" if package == "course2md" else package,
            version=version,
        )
        entries.append(_Entry(info, create))
    entries.sort(key=lambda entry: -entry.info.priority)
    return entries


def _registry() -> list[_Entry]:
    global _entries
    with _lock:
        if _entries is None:
            _errors.clear()
            _entries = _load()
        return _entries


def reload() -> None:
    """重新发现解析器，安装或删除插件后、以及测试中使用。"""
    global _entries
    with _lock:
        _entries = None


def available() -> list[ResolverInfo]:
    """按匹配顺序返回已启用的解析器。"""
    return [entry.info for entry in _registry()]


def load_errors() -> list[str]:
    _registry()
    return list(_errors)


def find(source: str):
    """返回能处理该来源的解析器实例，没有则返回 None。"""
    for entry in _registry():
        resolver = entry.create()
        try:
            if resolver.matches(source):
                return resolver
        except Exception:  # noqa: BLE001  有缺陷的插件不应阻止其它解析器匹配
            continue
    return None


def by_name(name: str):
    for entry in _registry():
        if entry.info.name == name:
            return entry.create()
    return None


def get_resolver(source: str):
    """返回能处理该来源的解析器实例。"""
    resolver = find(source)
    if resolver is None:
        names = "、".join(info.label for info in available()) or "无"
        raise ResolverError(f"没有能处理该来源的解析器：{public_source(source)}（已启用：{names}）")
    return resolver


def _owner(source: str):
    """原始来源由匹配的解析器负责；``name:...`` 形式的公开描述交回同名解析器。"""
    resolver = find(source)
    if resolver is not None:
        return resolver
    prefix, separator, _ = source.partition(":")
    if separator and NAME_PATTERN.fullmatch(prefix) and len(prefix) > 1:
        return by_name(prefix)
    return None


def _hook(resolver, name: str):
    return getattr(resolver, name, None) or getattr(BaseResolver(), name)


def public_source(source: str) -> str:
    """可写入日志与产物的来源描述，带凭证的来源由对应解析器改写。"""
    resolver = find(source)
    try:
        return str(_hook(resolver, "public_source")(source)) if resolver else BaseResolver().public_source(source)
    except Exception:  # noqa: BLE001  改写失败时宁可隐藏整条来源，也不能泄露凭证
        return f"{getattr(resolver, 'name', 'source')}:（来源已隐藏）"


def course_identity(source: str) -> str:
    """跨凭证保持不变的课程标识；无法识别时退回来源本身。"""
    resolver = _owner(source)
    if resolver is None:
        return source
    try:
        return str(_hook(resolver, "course_identity")(source)) or source
    except Exception:  # noqa: BLE001
        return source


def view_options(resolver, source: str) -> list[ViewOption]:
    try:
        options = _hook(resolver, "view_options")(source) or []
    except Exception:  # noqa: BLE001
        return []
    return [option for option in options if isinstance(option, ViewOption)]

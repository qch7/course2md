"""按名称加载内置策略和 Python 包声明的画面筛选插件（规则见 :mod:`course2md.plugins`）。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from ..plugins import PluginRegistry
from .api import FrameSelector, SelectionOptions

ENTRY_POINT_GROUP = "course2md.frame_selectors"
NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

# 不缓存：插件可在节点运行中安装，能力检测与任务执行应立即看到
_registry: PluginRegistry[Callable[[SelectionOptions], FrameSelector]] = PluginRegistry(
    "画面筛选策略",
    ENTRY_POINT_GROUP,
    {
        "classroom": "course2md.frame_selectors.classroom:ContentSelector",
        "general": "course2md.frame_selectors.general:ChangeSelector",
    },
    validate=lambda factory: [] if callable(factory) else ["入口点须为接收 SelectionOptions 的工厂"],
    cached=False,
)


def builtin_names() -> list[str]:
    return list(_registry.builtins)


def plugin_names() -> list[str]:
    """已安装且名称唯一的第三方策略。"""
    return _registry.plugin_names()


@dataclass(frozen=True)
class SelectorInfo:
    """供网页与 doctor 展示的策略说明；label 与 description 取自工厂的同名属性，可省略。"""

    name: str
    label: str
    description: str
    builtin: bool


def available() -> list[SelectorInfo]:
    """本机可用的策略，内置在前。加载失败的插件不列出，原因见 :func:`load_errors`。"""
    found: list[SelectorInfo] = []
    for name in _registry.names():
        try:
            factory = _registry.get(name)
        except ValueError:
            continue
        found.append(
            SelectorInfo(
                name=name,
                label=str(getattr(factory, "label", "") or name),
                description=str(getattr(factory, "description", "") or ""),
                builtin=name in _registry.builtins,
            )
        )
    return found


def load_errors() -> list[str]:
    return _registry.errors()


def create_selector(name: str, options: SelectionOptions) -> FrameSelector:
    try:
        selector = _registry.get(name)(options)
    except Exception as exc:
        raise ValueError(f"无法加载画面筛选策略 {name!r}：{exc}") from exc
    if not isinstance(selector, FrameSelector):
        raise ValueError(f"无法加载画面筛选策略 {name!r}：工厂须返回提供 add、selected 和 report 的筛选器")
    return selector


def selected_keys(selector: FrameSelector, keys: list[float]) -> list[float]:
    selected = set(selector.selected())
    if not selected.issubset(keys):
        raise ValueError("画面筛选器返回了未采样的帧")
    return sorted(selected)

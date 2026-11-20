"""Agent 预设注册表：内置 DSH 与 Codex，第三方预设通过 ``course2md.agents`` 入口点登记。"""

from __future__ import annotations

from typing import Any

from ..plugins import PluginRegistry
from .api import AGENT_API_VERSION, AgentError, AgentPreset

ENTRY_POINT_GROUP = "course2md.agents"
# 未配置 COURSE2MD_AGENT 时，本机装有此预设就自动选用，否则使用内置整理
AUTO_PRESET = "dsh"


def _validate(target: Any) -> list[str]:
    if not (isinstance(target, type) and issubclass(target, AgentPreset)):
        return ["入口点须为 AgentPreset 子类"]
    if getattr(target, "api_version", None) != AGENT_API_VERSION:
        return [f"接口版本 {getattr(target, 'api_version', None)} 与 course2md 支持的 {AGENT_API_VERSION} 不一致"]
    return []


_registry: PluginRegistry[type[AgentPreset]] = PluginRegistry(
    "Agent 预设",
    ENTRY_POINT_GROUP,
    {"dsh": "course2md.agents.dsh:DshPreset", "codex": "course2md.agents.codex:CodexPreset"},
    validate=_validate,
)


def preset_names() -> list[str]:
    """已安装的预设名称，内置在前。"""
    return _registry.names()


def get_preset(name: str) -> AgentPreset:
    try:
        return _registry.get(name)()
    except ValueError as exc:
        raise AgentError(str(exc)) from None


def find_preset(name: str) -> AgentPreset | None:
    try:
        return get_preset(name)
    except AgentError:
        return None


def load_errors() -> list[str]:
    return _registry.errors()


def reload() -> None:
    _registry.reload()


def detect_agents() -> dict[str, str]:
    """返回本机可用的预设及其启动命令。"""
    found: dict[str, str] = {}
    for name in preset_names():
        preset = find_preset(name)
        prefix = preset.locate() if preset else None
        if prefix:
            found[name] = " ".join(prefix)
    return found


_MODEL_OPTIONS_CACHE: dict[str, dict] = {}


def agent_model_options(refresh: bool = False) -> dict[str, dict]:
    """本机各预设可选的提供商和模型，仅作网页提示；读取失败时省略该预设。"""
    if _MODEL_OPTIONS_CACHE and not refresh:
        return _MODEL_OPTIONS_CACHE
    options: dict[str, dict] = {}
    for name in preset_names():
        preset = find_preset(name)
        if preset is None:
            continue
        try:
            found = preset.model_options()
        except AgentError:
            found = None
        if found is not None:
            options[name] = found
    _MODEL_OPTIONS_CACHE.clear()
    _MODEL_OPTIONS_CACHE.update(options)
    return _MODEL_OPTIONS_CACHE

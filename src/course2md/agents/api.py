"""Agent 预设插件的稳定接口。第三方预设只应从这里导入。

一个预设描述如何定位并驱动某个命令行 Agent（如 DSH、Codex）：怎样找到可执行文件、
怎样拼装参数与标准输入、怎样把它的 JSON 事件转成网页事件、本机有哪些可选模型。
进程管理、超时、取消、日志与会话记录由 :func:`course2md.agents.runner.execute_agent` 统一处理。

示例::

    from course2md.agents.api import AgentPreset, AgentRequest, Invocation

    class MyAgent(AgentPreset):
        name = "myagent"
        label = "My Agent"
        template = ("{prefix}", "run", "-")

        def locate(self): ...
        def prepare(self, cmd, cfg, request): ...
        def decode_event(self, event): ...

在插件的 pyproject.toml 中登记入口点::

    [project.entry-points."course2md.agents"]
    myagent = "course2md_myagent:MyAgent"

此模块中的名称在 AGENT_API_VERSION 不变时保持兼容。
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..errors import Course2mdError

AGENT_API_VERSION = 1

Progress = Callable[[float, str], None]
AgentOutput = Callable[[dict], None]

# 提供商与模型名会写进 CLI 的配置覆盖，只接受常见标识字符。
MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-:/@]{0,127}")


class AgentError(Course2mdError):
    """Agent 调用失败，消息面向用户可读。"""


def split_command(text: str) -> list[str]:
    """按 Windows 习惯拆分命令行，保留反斜杠，并去掉包裹单个参数的引号。"""
    return [
        part[1:-1] if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'" else part
        for part in shlex.split(text, posix=False)
    ]


@dataclass
class AgentConfig:
    """Agent 后端配置。preset 选择预设，command 可覆盖启动参数。"""

    preset: str = ""
    command: list[str] = field(default_factory=list)
    timeout: int = 1800
    output_notes: str = "response.md"  # 输出文件名，由调用方按用途指定
    reasoning_effort: str = ""
    provider: str = ""  # 留空沿用 CLI 自身配置的提供商
    model: str = ""  # 留空沿用 CLI 自身配置的模型

    def __post_init__(self) -> None:
        for name in ("provider", "model"):
            value = (getattr(self, name) or "").strip()
            if value and not MODEL_NAME.fullmatch(value):
                label = "提供商" if name == "provider" else "模型"
                raise AgentError(f"Agent {label}名称无效：{value}（只能包含字母、数字和 . _ - : / @）")
            setattr(self, name, value)

    @property
    def label(self) -> str:
        """便于展示的提供商与模型，未指定的部分标为 CLI 默认。"""
        if not (self.provider or self.model):
            return ""
        return f"{self.provider or '默认提供商'} / {self.model or '默认模型'}"

    @property
    def native_schema(self) -> bool:
        """所选预设能否自行约束结构化输出。"""
        if not self.preset:
            return False
        from .registry import find_preset

        preset = find_preset(self.preset)
        return bool(preset and preset.native_schema)

    def resolved_command(self) -> list[str]:
        """启动命令；预设未安装或找不到 CLI 时返回空列表。"""
        if self.command:
            return self.command
        if not self.preset:
            return []
        from .registry import find_preset

        preset = find_preset(self.preset)
        return preset.command() if preset else []


@dataclass(frozen=True)
class AgentRequest:
    """一次调用的输入。structured 表示需要 JSON 事件流（会话、结构化输出或网页实时输出）。"""

    workspace: Path
    instruction: str
    input_text: str | None = None
    images: tuple[Path, ...] = ()
    schema: Path | None = None
    structured: bool = False
    resume_id: str | None = None


@dataclass
class Invocation:
    """预设拼装好的启动参数。stdin 为 None 时不连接标准输入。"""

    cmd: list[str]
    stdin: str | None
    env: dict[str, str] = field(default_factory=dict)


class AgentPreset:
    """预设基类。子类至少实现 :meth:`locate`、:meth:`prepare` 与 :meth:`decode_event`。

    template               启动参数模板，``{prefix}`` 换成 :meth:`locate` 的结果，
                           ``{prompt}`` 换成指令，``{output_notes}`` 换成输出文件名
    final_event_is_result  CLI 在 ``final`` 事件中给出完整结果（由宿主写入输出文件）
    native_schema          CLI 自己按 ``--output-schema`` 约束输出，提示词中不必再附 JSON Schema
    """

    api_version: ClassVar[int] = AGENT_API_VERSION
    name: ClassVar[str]
    label: ClassVar[str] = ""
    template: ClassVar[tuple[str, ...]] = ("{prefix}",)
    final_event_is_result: ClassVar[bool] = False
    native_schema: ClassVar[bool] = False

    def locate(self) -> list[str] | None:
        """返回启动 CLI 的命令前缀（可含多个词），未安装时返回 None。"""
        raise NotImplementedError

    def command(self) -> list[str]:
        prefix = self.locate()
        if not prefix:
            return []
        cmd: list[str] = []
        for part in self.template:
            if part == "{prefix}":
                cmd.extend(prefix)
            else:
                cmd.append(part)
        return cmd

    def prepare(self, cmd: list[str], cfg: AgentConfig, request: AgentRequest) -> Invocation:
        """在已替换占位符的模板上追加参数，决定标准输入与环境变量。"""
        raise NotImplementedError

    def decode_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        """把一条 JSON 事件转成网页事件（kind 为 session / status / assistant / tool / error）。

        返回 None 表示不认识，按普通日志处理。模型推理内容只呈现运行状态，不外传。
        """
        return None

    def model_options(self) -> dict[str, Any] | None:
        """本机可选的提供商与模型 ``{default_provider, default_model, providers}``，仅作网页提示。

        CLI 未安装时返回 None。
        """
        return None


def error_text(value: Any, depth: int = 0) -> str:
    """取出 CLI 错误事件中最内层的 message，接口错误常以 JSON 字符串嵌套在字段里。"""
    import json

    if depth > 4 or value is None:
        return ""
    if isinstance(value, dict):
        inner = value.get("error") if isinstance(value.get("error"), dict) else None
        return (
            error_text(inner or value.get("message") or value.get("error"), depth + 1)
            or json.dumps(value, ensure_ascii=False)[:300]
        )
    text = str(value).strip()
    if text.startswith("{"):
        try:
            return error_text(json.loads(text), depth + 1) or text
        except ValueError:
            return text
    return text


__all__ = [
    "AGENT_API_VERSION",
    "MODEL_NAME",
    "AgentConfig",
    "AgentError",
    "AgentOutput",
    "AgentPreset",
    "AgentRequest",
    "Invocation",
    "Progress",
    "error_text",
    "split_command",
]

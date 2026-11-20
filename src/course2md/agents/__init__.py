"""命令行 Agent（DSH、Codex 及第三方预设）的配置、发现与调用。

- :mod:`.api`       预设插件的稳定接口与 :class:`AgentConfig`
- :mod:`.registry`  预设注册表、本机检测、可选模型
- :mod:`.runner`    运行一次 CLI（进程、超时、取消、事件解码、会话记录）
- :mod:`.dsh` / :mod:`.codex`  内置预设
"""

from .api import (
    AGENT_API_VERSION,
    AgentConfig,
    AgentError,
    AgentOutput,
    AgentPreset,
    AgentRequest,
    Invocation,
    Progress,
    split_command,
)
from .registry import agent_model_options, detect_agents, find_preset, get_preset, preset_names
from .runner import decode_output, execute_agent

__all__ = [
    "AGENT_API_VERSION",
    "AgentConfig",
    "AgentError",
    "AgentOutput",
    "AgentPreset",
    "AgentRequest",
    "Invocation",
    "Progress",
    "agent_model_options",
    "decode_output",
    "detect_agents",
    "execute_agent",
    "find_preset",
    "get_preset",
    "preset_names",
    "split_command",
]

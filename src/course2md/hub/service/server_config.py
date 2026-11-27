"""网页端需要的服务配置：可用 Agent、默认值与画面筛选策略。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ...config import (
    AsrConfig,
    LlmConfig,
    default_agent,
    default_delete_video,
    default_detail,
    default_formats,
    default_frame_interval,
    default_frame_selector,
    default_frames,
    default_out_dir,
    default_term_start,
    env,
    find_model,
)
from ...core.accounts import LOCAL_USER, User
from ...jobs.manager import JobManager
from ...resolvers import (
    available,
)
from .common import has_local_node, server_mode


def _node_capabilities(manager: JobManager, user: User) -> list[tuple[str, set[str]]]:
    """能为该用户领取任务的在线节点及其能力，供网页列出“整个计算服务”实际能做的事。"""
    queue = getattr(manager, "queue", None)
    if queue is None:
        return []
    return [
        (item["name"] or item["id"], set(item["capabilities"]))
        for item in queue.workers()
        if item["online"] and item["owner_id"] in (None, user.id)
    ]


def available_agents(manager: JobManager, user: User = LOCAL_USER) -> dict[str, str]:
    """可选的整理 Agent：值为启动命令或提供它的节点名。

    服务器上的 Hub 本身通常不装 Agent，只看 Hub 自己会漏掉计算节点上的 dsh / codex，
    因此合并在线节点声明的 ``agent:<名称>`` 能力；本地应用另外保留本机检测结果。
    """
    from ...agents import detect_agents, preset_names

    found: dict[str, str] = {} if server_mode(manager) else dict(detect_agents())
    providers: dict[str, list[str]] = {}
    for node, capabilities in _node_capabilities(manager, user):
        for capability in sorted(capabilities):
            name = capability.removeprefix("agent:")
            # agent:custom 是 Hub 所在电脑上的自定义命令，不是可选预设
            if capability.startswith("agent:") and name != "custom":
                providers.setdefault(name, []).append(node)
    # 内置预设排在前面，节点上的插件预设随后，顺序稳定便于网页展示
    order = [name for name in preset_names() if name in providers] + sorted(set(providers) - set(preset_names()))
    for name in order:
        found.setdefault(name, "节点：" + "、".join(providers[name]))
    return found


def config_payload(manager: JobManager, user: User = LOCAL_USER) -> dict:
    from ...asr.audio import SAMPLE_RATE  # noqa: F401  确认依赖可用

    asr_env, llm_env = AsrConfig.from_env(), LlmConfig.from_env()
    from ...agents import agent_model_options, preset_names
    from ...agents.registry import AUTO_PRESET
    from ...frame_selectors.registry import available as available_selectors

    agents = available_agents(manager, user)
    agent_default = default_agent().preset
    if not agent_default and not env("COURSE2MD_AGENT") and AUTO_PRESET in agents:
        # 网页默认选内置模型会让没读图能力的节点接走整理；有节点能跑 Agent 时默认选它
        agent_default = AUTO_PRESET
    has_llm_key = bool(llm_env.resolved_key())
    if server_mode(manager):
        # 服务器自己的密钥不算数：模型接口由具备 llm-api 的节点承担
        has_llm_key = any("llm-api" in caps for _, caps in _node_capabilities(manager, user))

    return {
        "model": asr_env.model,
        "device": asr_env.device,
        "model_path": find_model(asr_env.model),
        "has_llm_key": has_llm_key,
        "llm_base": llm_env.base_url,
        "llm_model": llm_env.model,
        "llm_vision": llm_env.vision,
        "max_images": llm_env.max_images,
        "default_out": str(Path.cwd() / default_out_dir()),
        "term_start": default_term_start(),
        "default_formats": list(default_formats()),
        "proofread": True,
        "detail": default_detail(),
        "frames": default_frames(),
        "frame_interval": default_frame_interval(),
        "frame_selector": default_frame_selector(),
        "frame_selectors": [asdict(info) for info in available_selectors()],
        "delete_video": default_delete_video(),
        "startup_warnings": getattr(manager, "warnings", []),
        "formats": ["srt", "vtt", "txt", "md", "json"],
        "devices": ["cuda", "cpu"],
        "agents": agents,
        "agent_default": agent_default,
        "agent_models": agent_model_options(),
        "agent_choices": {
            name: {"provider": choice.provider, "model": choice.model}
            for name, choice in ((name, default_agent(name)) for name in preset_names())
        },
        "session_batch": True,
        "has_local_node": has_local_node(manager),
        "resolvers": [{"name": info.name, "label": info.label, "hint": info.source_hint} for info in available()],
    }


def resolve_frame_selector(manager: JobManager, requested: object) -> str:
    """校验网页选择的画面筛选策略；留空使用配置中的默认值。"""
    from ...frame_selectors.registry import NAME_PATTERN, available

    if requested is None or requested == "":
        return default_frame_selector()
    if not isinstance(requested, str) or not NAME_PATTERN.fullmatch(requested):
        raise ValueError("画面筛选策略名称无效")
    # 服务器上策略插件可以只装在计算节点，由 frame-selector:<名称> 能力派发；本地应用要求本机已安装
    if not server_mode(manager) and requested not in {info.name for info in available()}:
        names = "、".join(info.name for info in available())
        raise ValueError(f"未安装画面筛选策略「{requested}」，可选 {names}")
    return requested

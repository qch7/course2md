"""节点能力检测。

==============  ==============================================
asr             已安装 faster-whisper 与 PyAV，可抽音频与转写
frames          已安装 PyAV 与 OpenCV，可扫描画面
frames:decode   已安装 PyAV，可解码视频画面
frame-selector:<n> 可运行对应的画面筛选策略
llm-api         可调用 OpenAI 兼容接口（密钥从本机配置读取）
agent:<name>    本机可启动对应的 Agent 预设（DSH、Codex 或插件）
resolver:<n>    已安装对应的来源解析插件（含插件版本时写作 resolver:<n>@<版本>）
hub             与 Hub 共享输出目录（只有内置 Worker 具备）
node            独立节点（运行在 Hub 之外）。Hub 可能访问不了的网络请求（如列出课次）只派给它
==============  ==============================================
"""

from __future__ import annotations

import importlib.util

from ..core.queue import satisfies
from ..frame_selectors.registry import plugin_names

__all__ = ["detect_capabilities", "resolver_capabilities", "satisfies"]


def _installed(*modules: str) -> bool:
    return all(importlib.util.find_spec(name) is not None for name in modules)


def resolver_capabilities() -> set[str]:
    from ..resolvers import available

    caps = set()
    for info in available():
        caps.add(f"resolver:{info.name}")
        if info.version:
            caps.add(f"resolver:{info.name}@{info.version}")
    return caps


def detect_capabilities(hub: bool = False, agents: bool = True) -> set[str]:
    caps = {"llm-api", "frame-selector:general"}
    caps.update(f"frame-selector:{name}" for name in plugin_names())
    if _installed("faster_whisper", "av"):
        caps.add("asr")
    if _installed("cv2"):
        caps.add("frame-selector:classroom")
    if _installed("av"):
        caps.add("frames:decode")
        if "frame-selector:classroom" in caps:
            caps.add("frames")
    if agents:
        from ..agents import detect_agents

        caps.update(f"agent:{name}" for name in detect_agents())
    if hub:
        caps.update({"hub", "agent:custom"})
    else:
        caps.add("node")
    return caps | resolver_capabilities()

"""节点能力检测。

==============  ==============================================
asr             已安装 faster-whisper 与 PyAV，可抽音频与转写
frames          已安装 PyAV 与 OpenCV，可扫描画面
frames:decode   已安装 PyAV，可解码视频画面
frame-selector:<n> 可运行对应的画面筛选策略
llm-api         已配置 OpenAI 兼容接口密钥，或指向本机回环地址（如 Ollama / vLLM）
agent:<name>    本机可启动对应的 Agent 预设（DSH、Codex 或插件）
resolver:<n>    已安装对应的来源解析插件（含插件版本时写作 resolver:<n>@<版本>）
hub             与 Hub 同机运行并共享本地路径（本地应用或 server --with-node）
==============  ==============================================
"""

from __future__ import annotations

import importlib.util
import ipaddress
from urllib.parse import urlparse

from ..config import LlmConfig
from ..core.queue import satisfies
from ..frame_selectors.registry import plugin_names

__all__ = ["detect_capabilities", "resolver_capabilities", "satisfies"]


def _installed(*modules: str) -> bool:
    return all(importlib.util.find_spec(name) is not None for name in modules)


def _is_loopback_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _has_llm_api() -> bool:
    """只有本机实际配置了 API 密钥，或接口指向本机回环地址时才声明 llm-api 能力。"""
    llm = LlmConfig.from_env()
    return bool(llm.resolved_key()) or _is_loopback_url(llm.resolved_base_url())


def resolver_capabilities() -> set[str]:
    from ..resolvers import available

    caps = set()
    for info in available():
        caps.add(f"resolver:{info.name}")
        if info.version:
            caps.add(f"resolver:{info.name}@{info.version}")
    return caps


def detect_capabilities(hub: bool = False, agents: bool = True, llm_api: bool = True) -> set[str]:
    """llm_api 为 False 时不声明 llm-api：服务器形态的内置节点用它，
    让模型接口只由用户自己连入的节点承担，Hub 上的密钥不会悄悄接走校对与整理。"""
    caps = {"frame-selector:general"}
    if llm_api and _has_llm_api():
        caps.add("llm-api")
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
    return caps | resolver_capabilities()

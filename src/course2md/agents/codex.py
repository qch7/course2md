"""Codex 预设：``codex exec``，从标准输入接收文字并直接附加图像，结果写入输出文件。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, ClassVar

from .api import AgentConfig, AgentError, AgentPreset, AgentRequest, Invocation, error_text


def codex_prefix() -> list[str] | None:
    """定位 Codex CLI。Windows npm 安装通过 Node 启动，保留含空格的路径参数。"""
    requested = os.environ.get("COURSE2MD_CODEX", "").strip().strip('"')
    executable = shutil.which(requested or "codex")
    if executable is None and requested:
        path = Path(requested).expanduser()
        if not path.is_file():
            raise AgentError("COURSE2MD_CODEX 指向的启动文件不存在，请填写 Codex CLI 路径")
        executable = str(path.resolve())
    if executable is None:
        return None
    path = Path(executable)
    if path.suffix.lower() in (".cmd", ".ps1", ".bat"):
        script = path.parent / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        node = shutil.which("node")
        if script.is_file() and node:
            return [node, str(script)]
    return [executable]


def parse_codex_models(text: str) -> dict:
    """从 Codex config.toml 读取当前提供商、模型和已登记的提供商与配置档模型。"""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        return {"default_provider": "", "default_model": "", "providers": {}}
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {"default_provider": "", "default_model": "", "providers": {}}
    provider = str(data.get("model_provider") or "openai")
    model = str(data.get("model") or "")
    providers: dict[str, list[str]] = {name: [] for name in data.get("model_providers") or {}}
    providers.setdefault("openai", [])
    providers.setdefault(provider, [])
    if model:
        providers[provider].append(model)
    for profile in (data.get("profiles") or {}).values():
        if isinstance(profile, dict) and profile.get("model"):
            name = str(profile.get("model_provider") or provider)
            models = providers.setdefault(name, [])
            if profile["model"] not in models:
                models.append(str(profile["model"]))
    return {"default_provider": provider, "default_model": model, "providers": providers}


class CodexPreset(AgentPreset):
    name: ClassVar[str] = "codex"
    label: ClassVar[str] = "Codex"
    template: ClassVar[tuple[str, ...]] = (
        "{prefix}",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--output-last-message",
        "{output_notes}",
        "-",
    )
    native_schema: ClassVar[bool] = True

    def locate(self) -> list[str] | None:
        return codex_prefix()

    def prepare(self, cmd: list[str], cfg: AgentConfig, request: AgentRequest) -> Invocation:
        if cfg.reasoning_effort:
            cmd.extend(["--config", f"model_reasoning_effort={cfg.reasoning_effort}"])
        if request.schema is not None:
            cmd.extend(["--output-schema", str(request.schema.resolve()), "--config", "project_doc_max_bytes=0"])
        for path in request.images:
            cmd.extend(["--image", str(path.resolve())])
        if request.structured:
            cmd.append("--json")
        if request.resume_id:
            cmd.remove("-")
            cmd.extend(["resume", request.resume_id, "-"])
        flags: list[str] = []
        if cfg.model:
            flags.extend(["--model", cfg.model])
        if cfg.provider:
            flags.extend(["--config", f"model_provider={json.dumps(cfg.provider)}"])
        position = cmd.index("exec") + 1
        cmd[position:position] = flags
        return Invocation(cmd, request.input_text)

    def decode_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if kind == "thread.started":
            return {"kind": "session", "session_id": event.get("thread_id"), "text": "Codex 会话已连接"}
        if kind in ("turn.started", "turn.completed"):
            return {"kind": "status", "text": "开始整理" if kind == "turn.started" else "本轮输出完成"}
        item = event.get("item")
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type == "reasoning":
                return {"kind": "status", "text": "正在分析课程材料"}
            if item_type == "agent_message":
                return {"kind": "assistant", "text": item.get("text", ""), "item_id": item.get("id")}
            return {
                "kind": "tool",
                "text": str(item.get("command") or item.get("name") or item_type),
                "item_id": item.get("id"),
                "phase": kind,
            }
        if kind in ("error", "turn.failed"):
            return {"kind": "error", "text": error_text(event.get("message") or event.get("error")) or "Codex 运行失败"}
        return None

    def model_options(self) -> dict[str, Any] | None:
        try:
            if not codex_prefix():
                return None
        except AgentError:
            return None
        home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            text = (home / "config.toml").read_text(encoding="utf-8")
        except OSError:
            text = ""
        return parse_codex_models(text)

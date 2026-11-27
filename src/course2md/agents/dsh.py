"""DeepSeek Harness（DSH）预设：headless 模式，从标准输入接收任务，结果在 final 事件中。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar

from ..util.proc import NO_WINDOW
from .api import AgentConfig, AgentPreset, AgentRequest, Invocation, split_command

DSH_DEFAULT_ROOTS = (r"C:\Program Files\DeepSeek Harness",)
DSH_CLI_RELATIVE = r"resources\app.asar\dsh\node_modules\@deepseek-ai\dsh-desktop-host\lib\cli.js"
DSH_MODEL_PATCH = ".course2md-dsh-model.patch.yml"


def _dsh_registry_roots() -> list[str]:
    """从 Windows 安装记录查找自定义安装目录；其它平台返回空列表。"""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots: list[str] = []
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view) as key:
                    index = 0
                    while True:
                        try:
                            name = winreg.EnumKey(key, index)
                        except OSError:
                            break
                        index += 1
                        try:
                            with winreg.OpenKey(key, name) as entry:
                                if winreg.QueryValueEx(entry, "DisplayName")[0] != "DeepSeek Harness":
                                    continue
                                location = winreg.QueryValueEx(entry, "InstallLocation")[0]
                        except OSError:
                            continue
                        if isinstance(location, str) and location.strip():
                            roots.append(os.path.expandvars(location.strip().strip('"')))
            except OSError:
                continue
    return list(dict.fromkeys(roots))


def _dsh_roots() -> tuple[str, ...]:
    """DSH 桌面版安装目录：显式配置、默认目录、Windows 安装记录。"""
    roots = [os.environ.get("COURSE2MD_DSH_HOME", "").strip().strip('"')]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(str(Path(os.environ["LOCALAPPDATA"]) / "Programs" / "DeepSeek Harness"))
    roots.extend(DSH_DEFAULT_ROOTS)
    roots.extend(_dsh_registry_roots())
    return tuple(dict.fromkeys(root for root in roots if root))


def dsh_prefix() -> list[str] | None:
    """返回启动 dsh 的命令前缀（可含多个词）。"""
    env = os.environ.get("COURSE2MD_DSH", "").strip()
    if env:
        return split_command(env)

    which = shutil.which("dsh")
    if which:
        return [which]

    roots = _dsh_roots()
    for root in roots:
        exe = Path(root) / "DeepSeek Harness.exe"
        asar = Path(root) / "resources" / "app.asar"
        # cli.js 在 asar 归档里，Python 看不到实体文件，交给 Electron 解析
        if exe.is_file() and asar.is_file():
            return [str(exe), "--expose-internals", str(Path(root) / DSH_CLI_RELATIVE)]

    for root in roots:
        shim = Path(root) / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"
        if shim.is_file():
            return ["cmd", "/c", str(shim)]
    return None


def _electron_env(cmd: list[str]) -> dict[str, str]:
    """桌面版通过 Electron 运行 CLI，需要以 Node 模式启动。"""
    return {"ELECTRON_RUN_AS_NODE": "1"} if cmd and Path(cmd[0]).name.lower() == "deepseek harness.exe" else {}


def parse_dsh_models(dump: str) -> dict:  # noqa: PLR0912  历史遗留的长函数，拆分后移除
    """从 dsh --dump-config 的输出读取默认提供商与模型，以及 llm-pi-ai 中登记的提供商和模型。"""
    result: dict = {"default_provider": "", "default_model": "", "providers": {}}
    block = ""
    in_providers = False
    provider_indent = None
    current = ""
    for raw in dump.splitlines():
        if raw.startswith("- id: "):
            block, in_providers, provider_indent, current = raw[6:].strip(), False, None, ""
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if block == "agent-default-model":
            key, _, value = stripped.partition(":")
            if key in ("provider", "model") and value.strip():
                result[f"default_{key}"] = value.strip().strip("'\"")
        elif block == "llm-pi-ai":
            if stripped == "providers:":
                in_providers, provider_indent = True, None
                continue
            if not in_providers:
                continue
            if provider_indent is None and stripped.endswith(":") and " " not in stripped:
                provider_indent = indent
            if indent == provider_indent and stripped.endswith(":"):
                current = stripped[:-1].strip("'\"")
                result["providers"].setdefault(current, [])
            elif provider_indent is not None and indent < provider_indent:
                in_providers = False
            elif current and re.match(r"(?:- )?id: ", stripped):
                model = stripped.split("id:", 1)[1].strip().strip("'\"")
                if model and model not in result["providers"][current]:
                    result["providers"][current].append(model)
    if result["default_provider"]:
        models = result["providers"].setdefault(result["default_provider"], [])
        if result["default_model"] and result["default_model"] not in models:
            models.insert(0, result["default_model"])
    return result


class DshPreset(AgentPreset):
    name: ClassVar[str] = "dsh"
    label: ClassVar[str] = "DeepSeek Harness"
    template: ClassVar[tuple[str, ...]] = ("{prefix}", "headless", "{prompt}")
    final_event_is_result: ClassVar[bool] = True

    def locate(self) -> list[str] | None:
        return dsh_prefix()

    def prepare(self, cmd: list[str], cfg: AgentConfig, request: AgentRequest) -> Invocation:
        # 结果由宿主从标准输出保存，工具一律只读，免审批也不会改文件或执行修改命令。
        cmd[-1:] = ["--json", "--permission-mode", "read-only", "--approval-policy", "never"]
        if request.resume_id:
            cmd.extend(["--session-id", request.resume_id])
        cmd.append("-")
        if cfg.provider or cfg.model:
            # 通过 --patch 覆盖 agent-default-model 插件，未指定的部分沿用 CLI 自身配置
            lines = ["- id: agent-default-model", "  config:"]
            if cfg.provider:
                lines.append(f"    provider: {json.dumps(cfg.provider)}")
            if cfg.model:
                lines.append(f"    model: {json.dumps(cfg.model)}")
            patch = request.workspace / DSH_MODEL_PATCH
            patch.parent.mkdir(parents=True, exist_ok=True)
            patch.write_text("\n".join(lines) + "\n", encoding="utf-8")
            position = cmd.index("headless") + 1
            cmd[position:position] = ["--patch", str(patch.resolve())]
        return Invocation(cmd, f"{request.instruction}\n\n{request.input_text}", _electron_env(cmd))

    def decode_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event.get("type")
        if kind == "session":
            return {"kind": "session", "session_id": event.get("sessionId"), "text": "DSH 会话已连接"}
        if kind == "thinking":
            return {"kind": "status", "text": "正在分析课程材料"}
        if kind in ("text", "final"):
            return {
                "kind": "assistant",
                "text": event.get("text", ""),
                "final": kind == "final",
                "truncated": event.get("truncated", False),
            }
        if kind in ("tool_call", "tool_result"):
            return {
                "kind": "tool",
                "text": str(event.get("tool") or event.get("result") or "工具执行完成"),
                "item_id": event.get("callId"),
                "phase": kind,
            }
        if kind == "status":
            return {"kind": "status", "text": f"DSH · {event.get('phase', '')}"}
        if kind == "error":
            return {"kind": "error", "text": str(event.get("message", "DSH 运行失败"))}
        return None

    def model_options(self) -> dict[str, Any] | None:
        prefix = dsh_prefix()
        if not prefix:
            return None
        environment = {**os.environ, **_electron_env(prefix)}
        try:
            dump = subprocess.run(
                [*prefix, "--profile", "headless", "--dump-config"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=environment,
                creationflags=NO_WINDOW,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            dump = ""
        return parse_dsh_models(dump)

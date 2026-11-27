"""运行一次 Agent CLI：进程管理、超时与取消、输出解码、日志与会话记录。

与具体 CLI 有关的部分（参数、标准输入、事件格式）由预设（:mod:`.api`）提供。
未选预设的自定义命令不连接标准输入，输出按普通日志处理。
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

from ..util.proc import NO_WINDOW, kill_tree
from ..util.winacl import ACL_FAILURE_MARKER, ACL_HINT
from .api import AgentConfig, AgentError, AgentOutput, AgentPreset, AgentRequest, Invocation, Progress
from .registry import find_preset

# 并行调用时只让一个 CLI 处于启动阶段：DSH 每次启动都会改写 ~/.dsh 下同一份配置，
# 同时启动可能互相占用文件。CLI 输出第一行（或超过宽限时间）后放行下一个。
_STARTUP_LOCK = threading.Lock()
STARTUP_GRACE_SECONDS = 15.0

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def decode_output(raw: str, preset: AgentPreset | None) -> dict:
    """将 CLI 的一行输出转为网页事件，不认识的内容按日志处理。"""
    text = _ANSI.sub("", raw).strip()
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return {"kind": "log", "text": text}
    if not isinstance(event, dict) or preset is None:
        return {"kind": "log", "text": text}
    return preset.decode_event(event) or {"kind": "log", "text": text}


def execute_agent(  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
    workspace: Path,
    cfg: AgentConfig,
    progress: Progress,
    cancel: threading.Event | None = None,
    *,
    instruction: str,
    input_text: str | None = None,
    images: list[Path] | None = None,
    schema: Path | None = None,
    on_output: AgentOutput | None = None,
    resume_id: str | None = None,
) -> str:
    """执行 CLI 并保存产物及会话 ID。取消或超时终止本次进程组，原始输出保存在 agent.log。"""
    template = cfg.resolved_command()
    if not template:
        raise AgentError("未配置 Agent 命令")
    preset = find_preset(cfg.preset) if cfg.preset else None
    cmd = [part.replace("{prompt}", instruction).replace("{output_notes}", cfg.output_notes) for part in template]
    if preset is not None:
        request = AgentRequest(
            workspace=workspace,
            instruction=instruction,
            input_text=input_text,
            images=tuple(images or ()),
            schema=schema,
            structured=schema is not None or on_output is not None or resume_id is not None,
            resume_id=resume_id,
        )
        invocation = preset.prepare(cmd, cfg, request)
    else:
        invocation = Invocation(cmd, None)
    piped = invocation.stdin is not None or preset is not None
    environment = {**os.environ, **invocation.env}
    if cancel and cancel.is_set():
        raise AgentError("任务已取消")
    output = workspace / cfg.output_notes
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    _STARTUP_LOCK.acquire()
    try:
        process = subprocess.Popen(
            invocation.cmd,
            cwd=str(workspace),
            stdin=subprocess.PIPE if piped else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=NO_WINDOW,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        _STARTUP_LOCK.release()
        raise AgentError(f"无法启动 Agent，请检查 CLI 路径 {invocation.cmd[0]}") from exc
    lines: queue.Queue[str | None] = queue.Queue()
    started = threading.Event()

    def read_output() -> None:
        try:
            for line in process.stdout or ():
                started.set()
                lines.put(line)
        finally:
            started.set()
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    input_errors: list[OSError] = []

    def write_input() -> None:
        try:
            if process.stdin is not None:
                with process.stdin:
                    process.stdin.write(invocation.stdin or "")
        except OSError as exc:
            input_errors.append(exc)

    writer = threading.Thread(target=write_input, daemon=True) if piped else None
    if writer:
        writer.start()
    try:
        started.wait(STARTUP_GRACE_SECONDS)
    finally:
        _STARTUP_LOCK.release()
    deadline = time.monotonic() + cfg.timeout
    final_text = None
    last_assistant = None
    count = 0
    acl_warned = False
    last_error = ""
    try:
        with (workspace / "agent.log").open("a", encoding="utf-8") as log:
            while True:
                if cancel and cancel.is_set():
                    raise AgentError("任务已取消")
                if time.monotonic() >= deadline:
                    raise AgentError(f"Agent 整理超过 {cfg.timeout} 秒，请调整超时或缩短课程")
                try:
                    raw = lines.get(timeout=0.15)
                except queue.Empty:
                    continue
                if raw is None:
                    break
                log.write(raw)
                log.flush()
                if not acl_warned and ACL_FAILURE_MARKER in raw:
                    # 受限令牌下 CLI 无法授权工作目录，读图仍可继续，提示用户在普通终端重启服务。
                    acl_warned = True
                    log.write(f"[course2md] {ACL_HINT}\n")
                    if on_output:
                        on_output({"kind": "error", "text": ACL_HINT})
                    progress(0.0, ACL_HINT[:140])
                event = decode_output(raw, preset)
                if event["kind"] == "error" and event.get("text"):
                    last_error = str(event["text"])
                elif event["kind"] == "log" and re.match(r"(dsh: |Error: |error: )", event.get("text", "")):
                    last_error = event["text"]
                if event["kind"] == "session" and isinstance(event.get("session_id"), str):
                    if resume_id and event["session_id"] != resume_id:
                        raise AgentError("CLI 返回了不同的会话 ID，本轮已停止，请重试")
                    (workspace / "agent-session.json").write_text(
                        json.dumps({"backend": cfg.preset, "session_id": event["session_id"]}) + "\n",
                        encoding="utf-8",
                    )
                if preset is not None and preset.final_event_is_result and event.get("final"):
                    final_text = event["text"]
                    if final_text == last_assistant:
                        continue
                if event["kind"] == "assistant":
                    last_assistant = event["text"]
                if event.get("text"):
                    if on_output:
                        on_output(event)
                    count += 1
                    if count % 8 == 1:
                        progress(0.0, str(event["text"])[:140])
        try:
            code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AgentError(f"Agent 整理超过 {cfg.timeout} 秒，请调整超时或缩短课程") from None
    finally:
        kill_tree(process)
        process.wait()
        reader.join(timeout=10)
        if writer:
            writer.join(timeout=10)
        if process.stdout:
            process.stdout.close()
    if code != 0:
        reason = f"：{last_error[:300]}" if last_error else ""
        choice = f"（{cfg.label}）" if cfg.label else ""
        raise AgentError(f"Agent{choice} 退出码 {code}{reason}，详情见工作目录中的 agent.log")
    if input_errors:
        raise AgentError("Agent 未能完整接收课程输入，请查看 agent.log 后重试") from input_errors[0]
    if isinstance(final_text, str):
        output.write_text(final_text, encoding="utf-8")
    if not output.is_file() or not output.read_text(encoding="utf-8").strip():
        raise AgentError(f"Agent 没有产出有效内容（{workspace.name}），请查看 agent.log")
    return output.read_text(encoding="utf-8")

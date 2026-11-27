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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

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


def _build_invocation(
    workspace: Path,
    cfg: AgentConfig,
    preset: AgentPreset | None,
    template: list[str],
    *,
    instruction: str,
    input_text: str | None,
    images: list[Path] | None,
    schema: Path | None,
    on_output: AgentOutput | None,
    resume_id: str | None,
) -> Invocation:
    """把命令模板展开成实际调用；有预设时交给预设补全参数、标准输入与环境变量。"""
    cmd = [part.replace("{prompt}", instruction).replace("{output_notes}", cfg.output_notes) for part in template]
    if preset is None:
        return Invocation(cmd, None)
    request = AgentRequest(
        workspace=workspace,
        instruction=instruction,
        input_text=input_text,
        images=tuple(images or ()),
        schema=schema,
        structured=schema is not None or on_output is not None or resume_id is not None,
        resume_id=resume_id,
    )
    return preset.prepare(cmd, cfg, request)


@dataclass
class _AgentProcess:
    """已启动的 CLI 进程及其读写线程，供输出循环与收尾共用。"""

    process: subprocess.Popen[str]
    lines: queue.Queue[str | None]
    reader: threading.Thread
    writer: threading.Thread | None
    input_errors: list[OSError] = field(default_factory=list)


def _start_process(invocation: Invocation, workspace: Path, piped: bool) -> _AgentProcess:
    """在启动锁内拉起进程与读写线程，等首行输出或宽限时间结束后放行下一个启动。"""
    environment = {**os.environ, **invocation.env}
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
    return _AgentProcess(process, lines, reader, writer, input_errors)


@dataclass
class _OutputStream:
    """逐行处理 CLI 输出：写日志、转发事件、记录会话 ID 与最后的错误。"""

    workspace: Path
    cfg: AgentConfig
    preset: AgentPreset | None
    progress: Progress
    on_output: AgentOutput | None
    resume_id: str | None
    final_text: object = None
    last_assistant: object = None
    count: int = 0
    acl_warned: bool = False
    last_error: str = ""

    def handle_line(self, raw: str, log: TextIO) -> None:
        """处理一行输出；会话 ID 与请求恢复的不一致时抛 AgentError。"""
        log.write(raw)
        log.flush()
        if not self.acl_warned and ACL_FAILURE_MARKER in raw:
            self._warn_acl(log)
        event = decode_output(raw, self.preset)
        self._track_error(event)
        if event["kind"] == "session" and isinstance(event.get("session_id"), str):
            self._record_session(event["session_id"])
        if self.preset is not None and self.preset.final_event_is_result and event.get("final"):
            self.final_text = event["text"]
            if self.final_text == self.last_assistant:
                return
        if event["kind"] == "assistant":
            self.last_assistant = event["text"]
        if event.get("text"):
            if self.on_output:
                self.on_output(event)
            self.count += 1
            if self.count % 8 == 1:
                self.progress(0.0, str(event["text"])[:140])

    def _warn_acl(self, log: TextIO) -> None:
        # 受限令牌下 CLI 无法授权工作目录，读图仍可继续，提示用户在普通终端重启服务。
        self.acl_warned = True
        log.write(f"[course2md] {ACL_HINT}\n")
        if self.on_output:
            self.on_output({"kind": "error", "text": ACL_HINT})
        self.progress(0.0, ACL_HINT[:140])

    def _track_error(self, event: dict) -> None:
        if event["kind"] == "error" and event.get("text"):
            self.last_error = str(event["text"])
        elif event["kind"] == "log" and re.match(r"(dsh: |Error: |error: )", event.get("text", "")):
            self.last_error = event["text"]

    def _record_session(self, session_id: str) -> None:
        if self.resume_id and session_id != self.resume_id:
            raise AgentError("CLI 返回了不同的会话 ID，本轮已停止，请重试")
        (self.workspace / "agent-session.json").write_text(
            json.dumps({"backend": self.cfg.preset, "session_id": session_id}) + "\n",
            encoding="utf-8",
        )


def _pump_output(
    lines: queue.Queue[str | None],
    log: TextIO,
    stream: _OutputStream,
    cancel: threading.Event | None,
    deadline: float,
    timeout: float,
) -> None:
    """消费输出队列直到 CLI 关闭输出；取消与超时在每次轮询时检查。"""
    while True:
        if cancel and cancel.is_set():
            raise AgentError("任务已取消")
        if time.monotonic() >= deadline:
            raise AgentError(f"Agent 整理超过 {timeout} 秒，请调整超时或缩短课程")
        try:
            raw = lines.get(timeout=0.15)
        except queue.Empty:
            continue
        if raw is None:
            break
        stream.handle_line(raw, log)


def _stop_process(running: _AgentProcess) -> None:
    """无论成败都终止进程树并回收读写线程。"""
    kill_tree(running.process)
    running.process.wait()
    running.reader.join(timeout=10)
    if running.writer:
        running.writer.join(timeout=10)
    if running.process.stdout:
        running.process.stdout.close()


def execute_agent(
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
    invocation = _build_invocation(
        workspace,
        cfg,
        preset,
        template,
        instruction=instruction,
        input_text=input_text,
        images=images,
        schema=schema,
        on_output=on_output,
        resume_id=resume_id,
    )
    piped = invocation.stdin is not None or preset is not None
    if cancel and cancel.is_set():
        raise AgentError("任务已取消")
    output = workspace / cfg.output_notes
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    running = _start_process(invocation, workspace, piped)
    deadline = time.monotonic() + cfg.timeout
    stream = _OutputStream(workspace, cfg, preset, progress, on_output, resume_id)
    try:
        with (workspace / "agent.log").open("a", encoding="utf-8") as log:
            _pump_output(running.lines, log, stream, cancel, deadline, cfg.timeout)
        try:
            code = running.process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise AgentError(f"Agent 整理超过 {cfg.timeout} 秒，请调整超时或缩短课程") from None
    finally:
        _stop_process(running)
    if code != 0:
        reason = f"：{stream.last_error[:300]}" if stream.last_error else ""
        choice = f"（{cfg.label}）" if cfg.label else ""
        raise AgentError(f"Agent{choice} 退出码 {code}{reason}，详情见工作目录中的 agent.log")
    if running.input_errors:
        raise AgentError("Agent 未能完整接收课程输入，请查看 agent.log 后重试") from running.input_errors[0]
    if isinstance(stream.final_text, str):
        output.write_text(stream.final_text, encoding="utf-8")
    if not output.is_file() or not output.read_text(encoding="utf-8").strip():
        raise AgentError(f"Agent 没有产出有效内容（{workspace.name}），请查看 agent.log")
    return output.read_text(encoding="utf-8")

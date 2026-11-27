"""基于当前课程记录继续问答，校验修改后同步保存两版笔记。"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from jsonschema import ValidationError, validate

from ..agents import AgentConfig, AgentError, AgentOutput, execute_agent
from ..asr.formats import Segment
from ..config import LlmConfig
from ..frames.sampling import Frame
from ..util.job_refs import from_job_refs
from ..util.text import hhmmss
from .lesson import (
    LESSON_SCHEMA,
    _object,
    _parse_lesson,
    _render,
    apply_figure_redirects,
    collect_evidence,
    save_note_pair,
)
from .llm import ChatClient, LlmError, parse_json_reply
from .prompts import LESSON_SYSTEM
from .vision.visuals import compact_readings, load_redirects, read_visuals

REPLY_SCHEMA = _object(
    {
        "reply": {"type": "string", "minLength": 1},
        "lesson": {"anyOf": [LESSON_SCHEMA, {"type": "null"}]},
    }
)


@dataclass
class Reply:
    """本轮可显示的回复，以及两版笔记是否已更新。"""

    text: str
    updated: bool


def load_context(workspace: Path) -> tuple[dict, dict, list[Segment], dict[str, Frame]]:
    """加载整理时保存的课程材料（context.json）与最新的课程记录。"""
    context_path = workspace / "context.json"
    if not context_path.is_file():
        raise LlmError("缺少课程材料，请重新整理该课程后继续对话")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    # 队列生成的工作区位于 <job>/notes/agent-workspace/<id>。
    if any(str(item["path"]).startswith("job://") for item in context["figures"].values()):
        context["figures"] = from_job_refs(context["figures"], workspace.parents[2])
    segments = [Segment(**item) for item in context["segments"]]
    if not segments:
        raise LlmError("缺少课程转写材料，请重新整理该课程后继续对话")
    figures = {key: Frame(item["ts"], Path(item["path"])) for key, item in context["figures"].items()}
    record_path = next(
        (
            workspace / name
            for name in ("lesson.current.json", "lesson.reviewed.json", "lesson.json")
            if (workspace / name).is_file()
        ),
        None,
    )
    if record_path is None:
        raise LlmError("缺少课程记录，请重新整理该课程后继续对话")
    record = _parse_lesson(record_path.read_text(encoding="utf-8"), segments, figures, collect_evidence(segments))
    return context, asdict(record), segments, figures


def _resume_id(workspace: Path, agent: AgentConfig) -> str | None:
    """读取同一后端留下的 CLI 会话 ID，记录损坏时按新会话处理。"""
    session_path = workspace / "agent-session.json"
    if not session_path.is_file():
        return None
    try:
        session = json.loads(session_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if session.get("backend") != agent.preset or not isinstance(session.get("session_id"), str):
        return None
    return session["session_id"]


def continue_notes(  # noqa: PLR0915  历史遗留的长函数，拆分后移除
    workspace: Path,
    message: str,
    history: list[dict],
    detailed_path: Path,
    cfg: LlmConfig,
    agent: AgentConfig,
    on_output: AgentOutput | None = None,
    cancel: threading.Event | None = None,
    client: ChatClient | None = None,
) -> Reply:
    """续接 CLI 会话或课程上下文。问答保留现有文件，修改通过引用校验后写出两版和 ZIP。"""
    context, record, segments, figures = load_context(workspace)
    turn = workspace / "turns" / uuid.uuid4().hex
    turn.mkdir(parents=True)
    schema = turn / "schema.json"
    schema.write_text(json.dumps(REPLY_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    evidence = collect_evidence(segments)
    use_agent = bool(agent.resolved_command())
    resume_id = _resume_id(workspace, agent) if use_agent else None
    header = (
        f"{LESSON_SYSTEM}\n\n你正在继续处理这门课程。回答用户的问题，或按用户要求修改当前课程记录。"
        "普通问答的 lesson 输出 null。修改笔记时，lesson 输出修改后的完整课程记录，reply 简要说明修改内容。"
        "保留课程证据，推导补充应标明依据。用户可以调整详略、组织方式与复习问题。"
        "回复只输出符合本轮 Schema 的 JSON，宿主程序负责校验和写入 Markdown。\n"
        f"本轮 JSON Schema\n{json.dumps(REPLY_SCHEMA, ensure_ascii=False)}\n"
        f"当前课程记录\n{json.dumps(record, ensure_ascii=False)}\n"
    )

    def full_instructions() -> str:
        readings, _ = read_visuals(
            figures, cfg, agent, workspace, lambda fraction, text: None, cancel, client, on_output
        )
        redirects = load_redirects(workspace)
        index = {key: frame.label for key, frame in figures.items() if key not in redirects}
        canonical = [page for page in readings if page["figure_id"] not in redirects]
        return (
            f"{header}"
            f"教师原话证据\n{json.dumps([asdict(item) for item in evidence], ensure_ascii=False)}\n"
            f"图像索引\n{json.dumps(index, ensure_ascii=False)}\n"
            f"逐张读取的画面证据\n{compact_readings(canonical)}\n"
            f"对话记录\n{json.dumps(history, ensure_ascii=False)}\n"
            f"用户本轮要求\n{message}\n\n原始课程转写\n"
            + "\n".join(f"[{hhmmss(item.start)}] {item.text}" for item in segments)
        )

    if resume_id:
        # 续接的会话已经读过转写、原话证据、画面记录和此前的对话，只发送会变化的部分。
        instructions = (
            f"{header}"
            "课程转写、教师原话证据、图像索引、画面读取记录与此前对话均已在本会话前文提供，"
            "引用时间戳和证据编号仍须与前文一致。\n"
            f"用户本轮要求\n{message}\n"
        )
    else:
        instructions = full_instructions()
    (turn / "request.md").write_text(instructions + "\n", encoding="utf-8")
    output = turn / "response.json"
    if use_agent:

        def run(text: str, session: str | None) -> str:
            return execute_agent(
                workspace,
                replace(agent, output_notes=output.relative_to(workspace).as_posix()),
                lambda _fraction, _message: None,
                cancel,
                instruction="Read the supplied course context. Return the requested JSON as the final reply.",
                input_text=text,
                schema=schema,
                on_output=on_output,
                resume_id=session,
            )

        try:
            response = run(instructions, resume_id)
        except AgentError:
            if not resume_id or (cancel and cancel.is_set()):
                raise
            # 会话可能已过期或被清理：改用新会话并发送完整课程材料。
            (workspace / "agent-session.json").unlink(missing_ok=True)
            instructions = full_instructions()
            (turn / "request.md").write_text(instructions + "\n", encoding="utf-8")
            response = run(instructions, None)
    else:
        client = client or ChatClient(cfg)
        response = client.chat([{"role": "user", "content": instructions}])
        output.write_text(response, encoding="utf-8")
        if on_output:
            on_output({"kind": "assistant", "text": response})
    try:
        data = parse_json_reply(response)
        validate(data, REPLY_SCHEMA)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LlmError("本轮回复格式无效，现有笔记已保留，可重新发送修改要求") from exc
    if cancel and cancel.is_set():
        raise AgentError("本轮对话已取消")
    if data["lesson"] is not None:
        updated = _parse_lesson(json.dumps(data["lesson"]), segments, figures, evidence)
        apply_figure_redirects(updated, load_redirects(workspace))
        pair = _render(context["title"], updated, figures, evidence)
        save_note_pair(pair, detailed_path)
        current = workspace / "lesson.current.json"
        temporary = turn / "lesson.current.json.part"
        temporary.write_text(json.dumps(asdict(updated), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(current)
    return Reply(data["reply"], data["lesson"] is not None)

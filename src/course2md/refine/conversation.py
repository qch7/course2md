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
    Evidence,
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


@dataclass
class _Turn:
    """一轮对话共用的材料与运行参数，避免在各步骤间逐项传递。"""

    workspace: Path
    turn: Path
    message: str
    history: list[dict]
    cfg: LlmConfig
    agent: AgentConfig
    on_output: AgentOutput | None
    cancel: threading.Event | None
    client: ChatClient | None
    segments: list[Segment]
    figures: dict[str, Frame]
    evidence: list[Evidence]
    header: str


def _turn_header(record: dict) -> str:
    """每轮请求共有的开头：系统提示、本轮 Schema 与当前课程记录。"""
    return (
        f"{LESSON_SYSTEM}\n\n你正在继续处理这门课程。回答用户的问题，或按用户要求修改当前课程记录。"
        "普通问答的 lesson 输出 null。修改笔记时，lesson 输出修改后的完整课程记录，reply 简要说明修改内容。"
        "保留课程证据，推导补充应标明依据。用户可以调整详略、组织方式与复习问题。"
        "回复只输出符合本轮 Schema 的 JSON，宿主程序负责校验和写入 Markdown。\n"
        f"本轮 JSON Schema\n{json.dumps(REPLY_SCHEMA, ensure_ascii=False)}\n"
        f"当前课程记录\n{json.dumps(record, ensure_ascii=False)}\n"
    )


def _full_instructions(turn: _Turn) -> str:
    """未能续接会话时发送的完整请求：读取画面记录并附上全部课程材料。"""
    readings, _ = read_visuals(
        turn.figures,
        turn.cfg,
        turn.agent,
        turn.workspace,
        lambda fraction, text: None,
        turn.cancel,
        turn.client,
        turn.on_output,
    )
    redirects = load_redirects(turn.workspace)
    index = {key: frame.label for key, frame in turn.figures.items() if key not in redirects}
    canonical = [page for page in readings if page["figure_id"] not in redirects]
    return (
        f"{turn.header}"
        f"教师原话证据\n{json.dumps([asdict(item) for item in turn.evidence], ensure_ascii=False)}\n"
        f"图像索引\n{json.dumps(index, ensure_ascii=False)}\n"
        f"逐张读取的画面证据\n{compact_readings(canonical)}\n"
        f"对话记录\n{json.dumps(turn.history, ensure_ascii=False)}\n"
        f"用户本轮要求\n{turn.message}\n\n原始课程转写\n"
        + "\n".join(f"[{hhmmss(item.start)}] {item.text}" for item in turn.segments)
    )


def _resumed_instructions(turn: _Turn) -> str:
    """续接的会话已经读过转写、原话证据、画面记录和此前的对话，只发送会变化的部分。"""
    return (
        f"{turn.header}"
        "课程转写、教师原话证据、图像索引、画面读取记录与此前对话均已在本会话前文提供，"
        "引用时间戳和证据编号仍须与前文一致。\n"
        f"用户本轮要求\n{turn.message}\n"
    )


def _run_agent(turn: _Turn, text: str, session: str | None) -> str:
    """在 Agent 中执行一轮对话，可带上续接的会话 ID。"""
    return execute_agent(
        turn.workspace,
        replace(turn.agent, output_notes=(turn.turn / "response.json").relative_to(turn.workspace).as_posix()),
        lambda _fraction, _message: None,
        turn.cancel,
        instruction="Read the supplied course context. Return the requested JSON as the final reply.",
        input_text=text,
        schema=turn.turn / "schema.json",
        on_output=turn.on_output,
        resume_id=session,
    )


def _agent_reply(turn: _Turn, instructions: str, resume_id: str | None) -> str:
    """调用 Agent；续接失败时清掉旧会话，改发完整课程材料重试一次。"""
    try:
        return _run_agent(turn, instructions, resume_id)
    except AgentError:
        if not resume_id or (turn.cancel and turn.cancel.is_set()):
            raise
        # 会话可能已过期或被清理：改用新会话并发送完整课程材料。
        (turn.workspace / "agent-session.json").unlink(missing_ok=True)
        instructions = _full_instructions(turn)
        (turn.turn / "request.md").write_text(instructions + "\n", encoding="utf-8")
        return _run_agent(turn, instructions, None)


def _model_reply(turn: _Turn, instructions: str) -> str:
    """调用内置模型并保存回复。"""
    client = turn.client or ChatClient(turn.cfg)
    response = client.chat([{"role": "user", "content": instructions}])
    (turn.turn / "response.json").write_text(response, encoding="utf-8")
    if turn.on_output:
        turn.on_output({"kind": "assistant", "text": response})
    return response


def _apply_lesson(turn: _Turn, title: str, lesson_data: dict, detailed_path: Path) -> None:
    """校验修改后的课程记录，写出两版笔记并原子更新当前记录。"""
    updated = _parse_lesson(json.dumps(lesson_data), turn.segments, turn.figures, turn.evidence)
    apply_figure_redirects(updated, load_redirects(turn.workspace))
    pair = _render(title, updated, turn.figures, turn.evidence)
    save_note_pair(pair, detailed_path)
    current = turn.workspace / "lesson.current.json"
    temporary = turn.turn / "lesson.current.json.part"
    temporary.write_text(json.dumps(asdict(updated), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(current)


def continue_notes(
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
    turn_dir = workspace / "turns" / uuid.uuid4().hex
    turn_dir.mkdir(parents=True)
    schema = turn_dir / "schema.json"
    schema.write_text(json.dumps(REPLY_SCHEMA, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    use_agent = bool(agent.resolved_command())
    resume_id = _resume_id(workspace, agent) if use_agent else None
    turn = _Turn(
        workspace=workspace,
        turn=turn_dir,
        message=message,
        history=history,
        cfg=cfg,
        agent=agent,
        on_output=on_output,
        cancel=cancel,
        client=client,
        segments=segments,
        figures=figures,
        evidence=collect_evidence(segments),
        header=_turn_header(record),
    )
    instructions = _resumed_instructions(turn) if resume_id else _full_instructions(turn)
    (turn_dir / "request.md").write_text(instructions + "\n", encoding="utf-8")
    response = _agent_reply(turn, instructions, resume_id) if use_agent else _model_reply(turn, instructions)
    try:
        data = parse_json_reply(response)
        validate(data, REPLY_SCHEMA)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LlmError("本轮回复格式无效，现有笔记已保留，可重新发送修改要求") from exc
    if cancel and cancel.is_set():
        raise AgentError("本轮对话已取消")
    if data["lesson"] is not None:
        _apply_lesson(turn, context["title"], data["lesson"], detailed_path)
    return Reply(data["reply"], data["lesson"] is not None)

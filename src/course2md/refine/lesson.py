"""提取可核对的课程记录，并由同一记录生成详细版与缩略版 Markdown。"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock
from jsonschema import ValidationError, validate

from ..agents import AgentConfig, AgentError, AgentOutput, execute_agent
from ..asr.formats import Segment, merge_paragraphs
from ..asr.frames import Frame
from ..config import LlmConfig
from ..frame_selectors.api import DEFAULT_SELECTOR
from ..util.text import hhmmss
from . import prompts
from .llm import ChatClient, LlmError, parse_json_reply
from .notes import build_chunks, summarize_chunks
from .visuals import compact_readings, load_redirects, read_visuals

Progress = Callable[[float, str], None]
EXAMPLE_CUE = re.compile(
    r"例题|单选题|这道题|题目|给定|选择.*正确|大家来做|例子|替换的方式|给出的.{0,4}文法|有.{0,4}文法"
)
EXAM_CUE = re.compile(r"考试|考点|必考|期末|期中考试|试卷|考核|考查|考察|出题")
NOTICE_CUE = re.compile(r"检查|作业|截止|提交|考核安排")
EMPHASIS_CUE = re.compile(r"记住|重点|注意|容易.*错|千万|一定要")


def _object(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


TEXT = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": TEXT}
TOPIC_SCHEMA = _object(
    {
        "title": NONEMPTY,
        "timestamp": NONEMPTY,
        "explanation": NONEMPTY,
        "key_points": {"type": "array", "items": NONEMPTY, "minItems": 1},
        "figure_ids": STRINGS,
    }
)
EXAMPLE_SCHEMA = _object(
    {
        "title": NONEMPTY,
        "timestamp": NONEMPTY,
        "question": NONEMPTY,
        "steps": {"type": "array", "items": NONEMPTY, "minItems": 1},
        "answer": NONEMPTY,
        "pitfalls": STRINGS,
        "figure_ids": STRINGS,
        "uncertainty": TEXT,
    }
)
LESSON_SCHEMA = _object(
    {
        "overview": TEXT,
        "topics": {"type": "array", "items": TOPIC_SCHEMA},
        "examples": {"type": "array", "items": EXAMPLE_SCHEMA},
        "teacher_points": {"type": "array", "items": _object({"evidence_id": TEXT, "meaning": TEXT})},
        "review_questions": STRINGS,
        "uncertainties": STRINGS,
    }
)


@dataclass
class Topic:
    """知识主题及用于缩略版的关键结论。"""

    title: str
    timestamp: str
    explanation: str
    key_points: list[str]
    figure_ids: list[str]


@dataclass
class Example:
    """课堂例题的题干、解题过程与证据画面。"""

    title: str
    timestamp: str
    question: str
    steps: list[str]
    answer: str
    pitfalls: list[str]
    figure_ids: list[str]
    uncertainty: str


@dataclass
class TeacherPoint:
    """引用程序提取的原话，模型仅补充说明。"""

    evidence_id: str
    meaning: str


@dataclass
class Evidence:
    """带有分类和上下文的课堂原话。"""

    id: str
    timestamp: str
    quote: str
    kind: str
    context: str


@dataclass
class Lesson:
    """通过 JSON Schema 与输入引用检查的课程内容。"""

    overview: str
    topics: list[Topic]
    examples: list[Example]
    teacher_points: list[TeacherPoint]
    review_questions: list[str]
    uncertainties: list[str]


@dataclass
class NotePair:
    """两版 Markdown 共用一份课程记录与已验证的引用。"""

    detailed: str
    brief: str
    figures: dict[str, Frame] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    model_calls: int = 1
    images_provided: int = 0
    workspace: Path | None = None


def collect_evidence(segments: list[Segment]) -> list[Evidence]:
    """按原文提取考试要求、课堂通知与教师强调，保留原话。"""
    evidence: list[Evidence] = []
    for index, segment in enumerate(segments):
        kind = ""
        if EXAM_CUE.search(segment.text):
            kind = "考试要求"
        elif NOTICE_CUE.search(segment.text):
            kind = "课堂通知"
        elif EMPHASIS_CUE.search(segment.text):
            kind = "课堂强调"
        if kind:
            context = "，".join(item.text for item in segments[max(0, index - 2) : index + 3])
            evidence.append(Evidence(f"e{index + 1}", hhmmss(segment.start), segment.text, kind, context))
    return evidence


def focus_timestamps(segments: list[Segment]) -> list[float]:
    """选取练习和考试提示附近的画面时间，连续提示合并为一次。"""
    first = next((segment for segment in segments if len(segment.text.strip()) >= 8), None)
    result: list[float] = [first.start + 5] if first is not None else []
    for segment in segments:
        if (EXAMPLE_CUE.search(segment.text) or EXAM_CUE.search(segment.text)) and (
            not result or segment.start + 5 - result[-1] >= 60
        ):
            result.append(segment.start + 5)
    return result


def _parse_lesson(text: str, segments: list[Segment], figures: dict[str, Frame], evidence: list[Evidence]) -> Lesson:
    try:
        data = parse_json_reply(text)
        validate(data, LESSON_SCHEMA)
    except (json.JSONDecodeError, ValidationError) as exc:
        raise LlmError("课程记录格式无效，原始回复已保存在整理工作目录，请重跑整理") from exc
    lesson = Lesson(
        data["overview"],
        [Topic(**item) for item in data["topics"]],
        [Example(**item) for item in data["examples"]],
        [TeacherPoint(**item) for item in data["teacher_points"]],
        data["review_questions"],
        data["uncertainties"],
    )
    if not lesson.topics or not lesson.review_questions:
        raise LlmError("课程记录缺少知识主题或复习问题，请重跑整理")
    anchors = {hhmmss(segment.start) for segment in segments} | {frame.label for frame in figures.values()}
    cited: list[Topic | Example] = [*lesson.topics, *lesson.examples]
    for item in cited:
        if item.timestamp not in anchors:
            raise LlmError(f"课程记录引用了输入中缺失的时间戳 {item.timestamp}")
        if set(item.figure_ids) - figures.keys():
            raise LlmError(f"课程记录引用了未知画面：{item.title}")
    if {point.evidence_id for point in lesson.teacher_points} - {item.id for item in evidence}:
        raise LlmError("课程记录引用了缺失的教师原话，请重跑整理")
    return lesson


def apply_figure_redirects(lesson: Lesson, redirects: dict[str, str]) -> None:
    """把引用的重复画面换成代表画面并去重，同一页只配一次图。"""
    cited: list[Topic | Example] = [*lesson.topics, *lesson.examples]
    for item in cited:
        item.figure_ids = list(dict.fromkeys(redirects.get(key, key) for key in item.figure_ids))


def _render(title: str, lesson: Lesson, figures: dict[str, Frame], evidence: list[Evidence]) -> NotePair:
    detailed = [f"# {title} · 详细版", "", f"> {lesson.overview}", "", "## 知识脉络", ""]
    brief = [f"# {title} · 缩略版", "", f"> {lesson.overview}", "", "## 核心结论", ""]
    used: set[str] = set()
    example_figures = {figure_id for item in lesson.examples for figure_id in item.figure_ids}

    def illustrations(ids: list[str], caption: str) -> list[str]:
        lines: list[str] = []
        for figure_id in ids:
            if figure_id in used:
                continue
            used.add(figure_id)
            frame = figures[figure_id]
            lines.extend(
                [
                    f"![{caption}，课堂画面 {frame.label}]({{{{figure:{figure_id}}}}})",
                    "",
                    f"*{caption} · [{frame.label}]*",
                    "",
                ]
            )
        return lines

    for topic in lesson.topics:
        detailed.extend([f"### {topic.title} [{topic.timestamp}]", "", topic.explanation, ""])
        detailed.extend(illustrations([item for item in topic.figure_ids if item not in example_figures], topic.title))
        brief.extend([f"### {topic.title} [{topic.timestamp}]", ""])
        brief.extend(f"- {point}" for point in topic.key_points[:3])
        brief.append("")
    detailed.extend(["## 例题与课堂练习", ""])
    brief.extend(["## 例题速查", ""])
    for index, example in enumerate(lesson.examples, 1):
        detailed.extend(
            [
                f"### 例题 {index} · {example.title} [{example.timestamp}]",
                "",
                "#### 题目与已知条件",
                "",
                example.question,
                "",
            ]
        )
        detailed.extend(illustrations(example.figure_ids, example.title))
        detailed.extend(["#### 解题过程", ""])
        for step, text in enumerate(example.steps, 1):
            indentation = " " * (len(str(step)) + 2)
            indented = text.replace("\n", "\n" + indentation)
            detailed.extend([f"{step}. {indented}", ""])
        detailed.extend(["#### 结论", "", example.answer, ""])
        if example.pitfalls:
            detailed.extend(["#### 易错点", "", *[f"- {item}" for item in example.pitfalls], ""])
        if example.uncertainty:
            detailed.extend([f"> 待核对 · {example.uncertainty}", ""])
        brief.extend([f"- **{example.title}** [{example.timestamp}]", f"  {example.answer}", ""])
    detailed.extend(["## 老师提及与课堂要求", ""])
    brief.extend(["## 老师提及", ""])
    by_id = {item.id: item for item in evidence}
    for point in lesson.teacher_points:
        source = by_id[point.evidence_id]
        detailed.extend([f"### {source.kind} [{source.timestamp}]", "", f"> {source.quote}", "", point.meaning, ""])
        brief.extend([f"- **{source.kind}** [{source.timestamp}] “{source.quote}” {point.meaning}", ""])
    detailed.extend(["## 复习清单", "", *[f"- [ ] {question}" for question in lesson.review_questions], ""])
    brief.extend(["## 复习清单", "", *[f"- [ ] {question}" for question in lesson.review_questions[:8]], ""])
    if lesson.uncertainties:
        detailed.extend(["## 待核对", "", *[f"- {item}" for item in lesson.uncertainties], ""])
    return NotePair(
        "\n".join(detailed).rstrip() + "\n",
        "\n".join(brief).rstrip() + "\n",
        {key: frame for key, frame in figures.items() if key in used},
    )


def build_note_pair(
    segments: list[Segment],
    title: str,
    meta: dict[str, str],
    cfg: LlmConfig,
    agent: AgentConfig,
    workspace: Path,
    frames: list[Frame] | None = None,
    progress: Progress = lambda fraction, message: None,
    cancel: threading.Event | None = None,
    client: ChatClient | None = None,
    on_output: AgentOutput | None = None,
    *,
    frame_selector: str = DEFAULT_SELECTOR,
) -> NotePair:
    """提取一份课程记录并渲染两版笔记。较长转写先提炼紧凑证据，产物失败保留原始回复。"""
    if not segments:
        raise ValueError("转写稿为空，无法整理")
    if cfg.max_images < 1:
        raise ValueError("COURSE2MD_MAX_IMAGES 必须大于 0")
    use_agent = bool(agent.resolved_command())
    started = time.perf_counter()
    workspace.mkdir(parents=True, exist_ok=True)
    images = sorted(frames or [], key=lambda frame: frame.ts)
    from ..asr.selection import select_frames

    def check_cancel() -> None:
        if cancel and cancel.is_set():
            raise AgentError("任务已取消")

    images, selection_report = select_frames(
        images, lambda fraction, message: progress(fraction * 0.05, message), check_cancel, selector_name=frame_selector
    )
    (workspace / "frame-selection.json").write_text(
        json.dumps(selection_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    figures = {f"f{index}": frame for index, frame in enumerate(images, 1)}
    evidence = collect_evidence(segments)
    transcript = "\n".join(f"[{hhmmss(item.start)}] {item.text}" for item in segments)
    model_calls = 1
    body = transcript
    if sum(len(item.text) for item in segments) > cfg.chunk_chars:
        chunks = build_chunks(merge_paragraphs(segments), cfg.chunk_chars)
        progress(0.05, f"较长课程先提取 {len(chunks)} 段例题与原话证据")
        client = client or ChatClient(cfg)
        extracted = summarize_chunks(chunks, title, client, progress)
        body = "\n\n".join(extracted)
        (workspace / "extracted-evidence.md").write_text(body, encoding="utf-8")
        model_calls += len(chunks)
    schema_path = workspace / "lesson.schema.json"
    schema_path.write_text(json.dumps(LESSON_SCHEMA, ensure_ascii=False, indent=2), encoding="utf-8")
    (workspace / "context.json").write_text(
        json.dumps(
            {
                "title": title,
                "meta": meta,
                "segments": [asdict(item) for item in segments],
                "figures": {key: {"ts": frame.ts, "path": str(frame.path.resolve())} for key, frame in figures.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    readings, reading_calls = read_visuals(
        figures,
        cfg,
        agent,
        workspace,
        lambda fraction, message: progress(0.15 + fraction * 0.55, message),
        cancel,
        client,
        on_output,
    )
    model_calls += reading_calls
    # 重复画面只把代表画面交给模型，避免同一页课件被当成不同的配图
    redirects = load_redirects(workspace)
    candidates = {key: frame for key, frame in figures.items() if key not in redirects}
    figure_list = "\n".join(f"{key} [{frame.label}]" for key, frame in candidates.items())
    evidence_list = json.dumps([vars(item) for item in evidence], ensure_ascii=False)
    task = (
        f"{prompts.LESSON_SYSTEM}\n\n课程 {title}\n课程信息 {json.dumps(meta, ensure_ascii=False)}\n\n"
        f"图像清单（截图 ID 与时间，重复画面已合并）\n{figure_list}\n\n"
        f"教师原话证据\n{evidence_list}\n"
    )
    if not (use_agent and agent.native_schema):
        task += "\nJSON Schema\n" + json.dumps(LESSON_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    if readings:
        canonical = [page for page in readings if page["figure_id"] not in redirects]
        task += f"\n逐张读取的画面证据（已读取 {len(readings)}/{len(figures)} 张）\n" + compact_readings(canonical)
        task += "\n逐条纳入画面中的知识与例题，合并重复内容。unreadable 中的具体缺失应写入待核对项。\n"
    response_path = workspace / "lesson.json"
    progress(0.75, f"整课提取例题与复习重点（{len(readings)}/{len(images)} 张画面已读取，生成两版笔记）")
    if use_agent:
        response_path.unlink(missing_ok=True)
        instruction = "Analyze the supplied course text and per-image reading evidence. Return the schema JSON as your final reply. The host saves the output."
        input_text = (
            f"{task}\n\n课堂转写或已提取的课堂证据\n{body}\n\n"
            "请直接分析提供的文字与逐图证据，最终回复仅输出符合 Schema 的 JSON。"
        )
        response = execute_agent(
            workspace,
            replace(agent, output_notes="lesson.json"),
            progress,
            cancel,
            instruction=instruction,
            input_text=input_text,
            schema=schema_path,
            on_output=on_output,
        )
    else:
        client = client or ChatClient(cfg)
        if images and not cfg.vision:
            task += "\n当前 API 收到的是图像时间索引，画面可用于回看参考，文字细节依据转写，待核对项保留。\n"
        response = client.chat([{"role": "user", "content": f"{task}\n\n课堂转写\n{body}"}])
        response_path.write_text(response, encoding="utf-8")
    lesson = _parse_lesson(response, segments, figures, evidence)
    apply_figure_redirects(lesson, redirects)
    for page in readings:
        if page["status"] == "unreadable":
            lesson.uncertainties.append(f"画面 [{figures[page['figure_id']].label}] {page['content']}")
    (workspace / "lesson.current.json").write_text(
        json.dumps(asdict(lesson), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    result = _render(title, lesson, figures, evidence)
    result.timings = {"organize": round(time.perf_counter() - started, 3)}
    result.model_calls = model_calls
    result.images_provided = len(readings)
    result.workspace = workspace
    progress(1.0, f"课程记录完成（{len(lesson.examples)} 道例题，{len(result.figures)} 张配图）")
    return result


def save_note_pair(pair: NotePair, detailed_path: Path) -> tuple[Path, Path]:
    """先保存配图，再分别原子替换两版 Markdown。失败时已提交的文件可能保留。"""
    detailed_path.parent.mkdir(parents=True, exist_ok=True)
    stem = detailed_path.name.removesuffix(".notes.md").removesuffix(".md")
    brief_path = detailed_path.with_name(f"{stem}.brief.md")
    asset_dir = detailed_path.parent / f"{stem}.assets"
    detailed = pair.detailed
    with FileLock(str(detailed_path) + ".lock"):
        assets: list[Path] = []
        for key, frame in pair.figures.items():
            asset_dir.mkdir(exist_ok=True)
            content = frame.path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()[:12]
            target = asset_dir / f"{frame.label.replace(':', '-')}-{digest}.jpg"
            if not target.exists():
                temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
                try:
                    temporary.write_bytes(content)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            assets.append(target)
            relative = target.relative_to(detailed_path.parent).as_posix()
            detailed = detailed.replace(f"{{{{figure:{key}}}}}", quote(relative, safe="/"))
        brief = pair.brief + f"\n[完整题干、推导、画面与待核对项](<{detailed_path.name}>)\n"
        for path, text in ((detailed_path, detailed), (brief_path, brief)):
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
            try:
                temporary.write_text(text, encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        bundle = detailed_path.with_name(f"{stem}.notes.zip")
        temporary = bundle.with_name(f".{bundle.name}.{uuid.uuid4().hex}.part")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in (detailed_path, brief_path, *assets):
                    archive.write(path, path.relative_to(detailed_path.parent).as_posix())
            temporary.replace(bundle)
        finally:
            temporary.unlink(missing_ok=True)
    return detailed_path, brief_path

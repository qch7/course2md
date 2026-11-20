"""两版笔记共用课程记录、教师原话与可携带的真实配图。"""

from __future__ import annotations

import json
import zipfile
from unittest.mock import Mock
from urllib.parse import unquote

import pytest

from course2md.agents import AgentConfig
from course2md.asr.formats import Segment
from course2md.asr.frames import Frame
from course2md.config import LlmConfig
from course2md.refine import lesson, visuals
from course2md.refine.llm import LlmError


@pytest.fixture
def material(tmp_path):
    image = tmp_path / "真实画面.jpg"
    image.write_bytes(b"classroom-image")
    segments = [Segment(0, 4, "我们下周开始期中检查。"), Segment(60, 65, "例题选 B，这个是考试重点。")]
    data = {
        "overview": "消除左递归与课堂练习。",
        "topics": [
            {
                "title": "直接左递归",
                "timestamp": "00:01:00",
                "explanation": "引入新的非终结符。",
                "key_points": ["保留 ε 候选式。"],
                "figure_ids": ["f1"],
            }
        ],
        "examples": [
            {
                "title": "消除左递归",
                "timestamp": "00:01:00",
                "question": "给定 `E→E+T|T`，选择正确改写。",
                "steps": ["识别 `α=+T`。", "写出 `E→TE′`。"],
                "answer": "选 B，`E′→+TE′|ε`。",
                "pitfalls": ["不能漏掉 ε。"],
                "figure_ids": ["f1"],
                "uncertainty": "选项 A 的末尾不清楚。",
            }
        ],
        "teacher_points": [
            {"evidence_id": "e1", "meaning": "下周开展期中检查。"},
            {"evidence_id": "e2", "meaning": "老师强调本题的考试要求。"},
        ],
        "review_questions": ["为什么需要 ε？"],
        "uncertainties": ["选项 A 末尾需要回看。"],
    }
    return segments, Frame(60, image), data


def test_one_record_renders_complete_examples_and_consistent_brief(material, tmp_path):
    segments, frame, data = material
    client = Mock()
    client.chat.return_value = json.dumps(data, ensure_ascii=False)
    workspace = tmp_path / "work"
    pair = lesson.build_note_pair(
        segments, "编译原理", {}, LlmConfig(), AgentConfig(), workspace, frames=[frame], client=client
    )
    assert client.chat.call_count == pair.model_calls == 1
    assert "识别 `α=+T`" in pair.detailed
    assert "识别 `α=+T`" not in pair.brief
    assert "选 B，`E′→+TE′|ε`" in pair.detailed and "选 B，`E′→+TE′|ε`" in pair.brief
    assert "### 课堂通知 [00:00:00]" in pair.detailed
    assert "### 考试要求 [00:01:00]" in pair.detailed
    assert "> 我们下周开始期中检查。" in pair.detailed
    assert pair.detailed.count("{{figure:f1}}") == 1
    assert pair.figures == {"f1": frame}
    assert "## 待核对" in pair.detailed and "## 待核对" not in pair.brief
    assert "当前 API 收到的是图像时间索引" in client.chat.call_args.args[0][0]["content"]
    assert (workspace / "lesson.json").is_file()


def test_portable_context_loads_figures_in_another_job_directory(material, tmp_path):
    from course2md.refine.conversation import load_context
    from course2md.util.job_refs import to_job_refs

    segments, frame, record = material
    root = tmp_path / "first"
    root.mkdir()
    image = root / "frames" / "page.jpg"
    image.parent.mkdir()
    image.write_bytes(frame.path.read_bytes())
    context = {
        "title": "课程",
        "segments": [vars(item) for item in segments],
        "figures": to_job_refs({"f1": {"ts": frame.ts, "path": str(image)}}, root),
    }
    workspace = tmp_path / "second" / "notes" / "agent-workspace" / "one"
    workspace.mkdir(parents=True)
    (workspace / "context.json").write_text(json.dumps(context), encoding="utf-8")
    (workspace / "lesson.json").write_text(json.dumps(record), encoding="utf-8")
    _, _, _, figures = load_context(workspace)
    assert figures["f1"].path == tmp_path / "second" / "frames" / "page.jpg"


def test_builtin_vision_attaches_image_data(material, tmp_path):
    segments, frame, data = material
    client = Mock()
    client.chat.side_effect = [
        json.dumps(
            {
                "pages": [
                    {"figure_id": "f1", "status": "readable", "content": "E→E+T|T，选 B。"},
                ]
            }
        ),
        json.dumps(data),
    ]
    pair = lesson.build_note_pair(
        segments, "课", {}, LlmConfig(vision=True), AgentConfig(), tmp_path / "work", frames=[frame], client=client
    )
    assert pair.images_provided == 1
    assert pair.model_calls == 2
    content = client.chat.call_args_list[0].args[0][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    final = client.chat.call_args.args[0][0]["content"]
    assert isinstance(final, str) and "E→E+T|T，选 B。" in final


def test_codex_receives_schema_all_images_and_raw_transcript(material, tmp_path, monkeypatch):
    segments, frame, data = material
    execute = Mock(return_value=json.dumps(data))
    read_images = Mock(
        return_value=json.dumps(
            {
                "pages": [
                    {"figure_id": "f1", "status": "readable", "content": "E→E+T|T，选 B。"},
                ]
            }
        )
    )
    monkeypatch.setattr(lesson, "execute_agent", execute)
    monkeypatch.setattr(visuals, "execute_agent", read_images)
    monkeypatch.setattr(AgentConfig, "resolved_command", lambda self: ["codex", "exec"])
    lesson.build_note_pair(
        segments, "课", {}, LlmConfig(), AgentConfig(preset="codex"), tmp_path / "work", frames=[frame]
    )
    call = execute.call_args
    assert call.args[1].output_notes == "lesson.json"
    assert read_images.call_args.kwargs["images"] == [frame.path]
    assert "images" not in call.kwargs
    assert call.kwargs["schema"].name == "lesson.schema.json"
    assert "例题选 B，这个是考试重点。" in call.kwargs["input_text"]
    assert "E→E+T|T，选 B。" in call.kwargs["input_text"]
    assert frame.path.as_posix() not in call.kwargs["input_text"]


def test_all_frames_survive_batching_and_selected_image_is_embedded(material, tmp_path):
    import re

    from PIL import Image, ImageDraw

    segments, _, data = material
    frames = []
    for index in range(14):
        path = tmp_path / f"slide-{index}.jpg"
        image = Image.new("RGB", (640, 360), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20 + index * 16, 70, 100 + index * 16, 95), fill="black")
        draw.rectangle((500 - index * 15, 200, 580 - index * 15, 225), fill="black")
        image.save(path)
        frames.append(Frame(index * 10, path))
    data["topics"][0]["figure_ids"] = ["f14"]
    data["examples"][0]["figure_ids"] = ["f14"]

    def respond(messages):
        content = messages[0]["content"]
        if isinstance(content, str):
            return json.dumps(data)
        ids = re.findall(r"^(f\d+) \[", content[0]["text"], re.M)
        return json.dumps(
            {"pages": [{"figure_id": key, "status": "readable", "content": f"{key} 的题干。"} for key in ids]}
        )

    client = Mock()
    client.chat.side_effect = respond
    workspace = tmp_path / "work"
    pair = lesson.build_note_pair(
        segments, "课", {}, LlmConfig(vision=True, max_images=6), AgentConfig(), workspace, frames=frames, client=client
    )
    assert pair.images_provided == 14 and pair.model_calls == 4
    context = json.loads((workspace / "context.json").read_text(encoding="utf-8"))
    assert len(context["figures"]) == 14
    assert pair.figures == {"f14": frames[-1]}
    detailed, _ = lesson.save_note_pair(pair, tmp_path / "notes" / "课.notes.md")
    assert "![" in detailed.read_text(encoding="utf-8")
    assert len(list(detailed.parent.rglob("*.jpg"))) == 1
    assert all(frame.path.is_file() for frame in frames)


def test_followup_reuses_readings_without_resending_images(material, tmp_path):
    from course2md.refine.conversation import continue_notes

    segments, frame, data = material
    client = Mock()
    client.chat.side_effect = [
        json.dumps(
            {
                "pages": [
                    {"figure_id": "f1", "status": "readable", "content": "E→E+T|T，选 B。"},
                ]
            }
        ),
        json.dumps(data),
    ]
    cfg = LlmConfig(vision=True)
    workspace = tmp_path / "work"
    pair = lesson.build_note_pair(segments, "课", {}, cfg, AgentConfig(), workspace, frames=[frame], client=client)
    detailed, _ = lesson.save_note_pair(pair, tmp_path / "课.notes.md")
    client.reset_mock()
    client.chat.side_effect = None
    client.chat.return_value = json.dumps({"reply": "选 B。", "lesson": None})
    reply = continue_notes(workspace, "本题选什么？", [], detailed, cfg, AgentConfig(), client=client)
    assert reply.text == "选 B。" and client.chat.call_count == 1
    content = client.chat.call_args.args[0][0]["content"]
    assert isinstance(content, str) and "E→E+T|T，选 B。" in content


def test_unreadable_screen_is_reported_in_notes(material, tmp_path):
    segments, frame, data = material
    client = Mock()
    client.chat.side_effect = [
        json.dumps(
            {
                "pages": [
                    {"figure_id": "f1", "status": "unreadable", "content": "公式下标模糊，需要核对。"},
                ]
            }
        ),
        json.dumps(data),
    ]
    pair = lesson.build_note_pair(
        segments, "课", {}, LlmConfig(vision=True), AgentConfig(), tmp_path / "work", frames=[frame], client=client
    )
    assert "画面 [00:01:00] 公式下标模糊，需要核对。" in pair.detailed


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("timestamp", "00:59:59", "时间戳"),
        ("figure_ids", ["missing"], "未知画面"),
    ],
)
def test_record_rejects_unknown_source_references(material, field, value, message):
    segments, frame, data = material
    data["examples"][0][field] = value
    with pytest.raises(LlmError, match=message):
        lesson._parse_lesson(json.dumps(data), segments, {"f1": frame}, lesson.collect_evidence(segments))


def test_record_rejects_invented_teacher_quote_and_invalid_json(material):
    segments, frame, data = material
    data["teacher_points"][0]["evidence_id"] = "invented"
    with pytest.raises(LlmError, match="教师原话"):
        lesson._parse_lesson(json.dumps(data), segments, {"f1": frame}, lesson.collect_evidence(segments))
    with pytest.raises(LlmError, match="格式无效"):
        lesson._parse_lesson("请提供转写稿", segments, {}, [])
    data["topics"] = []
    with pytest.raises(LlmError, match="知识主题"):
        lesson._parse_lesson(json.dumps(data), segments, {}, [])


def test_record_rejects_example_without_solution_steps(material):
    segments, frame, data = material
    data["examples"][0]["steps"] = []
    with pytest.raises(LlmError, match="格式无效"):
        lesson._parse_lesson(json.dumps(data), segments, {"f1": frame}, lesson.collect_evidence(segments))


def test_long_transcript_extracts_example_evidence_before_final_record(material, tmp_path):
    segments, frame, data = material
    client = Mock()
    client.chat.side_effect = ["[00:00:00] 下周期中检查。", "[00:01:00] 例题选 B。", json.dumps(data)]
    pair = lesson.build_note_pair(
        segments, "课", {}, LlmConfig(chunk_chars=8), AgentConfig(), tmp_path / "work", frames=[frame], client=client
    )
    assert client.chat.call_count == pair.model_calls == 3
    assert "题干" in client.chat.call_args_list[0].args[0][0]["content"]
    assert (tmp_path / "work" / "extracted-evidence.md").read_text(encoding="utf-8").count("例题") == 1


def test_note_bundle_keeps_relative_images_and_stable_previous_assets(material, tmp_path):
    segments, frame, data = material
    record = lesson._parse_lesson(json.dumps(data), segments, {"f1": frame}, lesson.collect_evidence(segments))
    pair = lesson._render("编译原理", record, {"f1": frame}, lesson.collect_evidence(segments))
    detailed, brief = lesson.save_note_pair(pair, tmp_path / "output" / "课堂 笔记.notes.md")
    text = detailed.read_text(encoding="utf-8")
    assert "{{figure:" not in text
    relative = text.split("](", 1)[1].split(")", 1)[0]
    asset = detailed.parent / unquote(relative)
    assert asset.read_bytes() == b"classroom-image"
    assert "课堂 笔记.notes.md" in brief.read_text(encoding="utf-8")
    with zipfile.ZipFile(detailed.with_suffix(".zip")) as archive:
        assert set(archive.namelist()) == {
            detailed.name,
            brief.name,
            asset.relative_to(detailed.parent).as_posix(),
        }
        assert archive.testzip() is None
    frame.path.write_bytes(b"updated-image")
    lesson.save_note_pair(pair, detailed)
    assert asset.read_bytes() == b"classroom-image"
    assert len(list(asset.parent.glob("*.jpg"))) == 2
    assert not list(detailed.parent.glob("*.part"))


def test_interrupted_image_save_can_be_retried(material, tmp_path, monkeypatch):
    from pathlib import Path

    segments, frame, data = material
    evidence = lesson.collect_evidence(segments)
    record = lesson._parse_lesson(json.dumps(data), segments, {"f1": frame}, evidence)
    pair = lesson._render("课程", record, {"f1": frame}, evidence)
    detailed = tmp_path / "output" / "课.notes.md"
    original_replace = Path.replace

    def fail_image_commit(path, target):
        if target.suffix == ".jpg":
            raise OSError("image commit interrupted")
        return original_replace(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_image_commit)
        with pytest.raises(OSError, match="interrupted"):
            lesson.save_note_pair(pair, detailed)
    assert not detailed.exists()
    assert not list(detailed.parent.rglob("*.jpg"))
    assert not list(detailed.parent.rglob("*.part"))
    lesson.save_note_pair(pair, detailed)
    assert detailed.is_file()
    assert next(detailed.parent.rglob("*.jpg")).read_bytes() == b"classroom-image"


def test_focus_times_cover_separate_examples_and_merge_adjacent_cues():
    segments = [
        Segment(10, 12, "开始讨论这个课程内容。"),
        Segment(70, 75, "来看一个例题。"),
        Segment(80, 85, "这道题继续。"),
        Segment(240, 245, "给出的这个文法。"),
    ]
    assert lesson.focus_timestamps(segments) == [15, 75, 245]


def test_followup_updates_both_notes_and_preserves_original_material(material, tmp_path):
    from course2md.refine.conversation import continue_notes

    segments, frame, data = material
    client = Mock()
    client.chat.return_value = json.dumps(data)
    workspace = tmp_path / "work"
    pair = lesson.build_note_pair(
        segments, "课程", {}, LlmConfig(), AgentConfig(), workspace, frames=[frame], client=client
    )
    detailed, brief = lesson.save_note_pair(pair, tmp_path / "notes" / "课.notes.md")
    data["topics"][0]["explanation"] = "补充 ε 用于终止递归。"
    data["topics"][0]["key_points"] = ["补充 ε 用于终止递归。"]
    client.chat.return_value = json.dumps({"reply": "已补充终止条件。", "lesson": data})
    reply = continue_notes(workspace, "解释终止条件", [], detailed, LlmConfig(), AgentConfig(), client=client)
    assert reply.updated and reply.text == "已补充终止条件。"
    assert "补充 ε 用于终止递归。" in detailed.read_text(encoding="utf-8")
    assert "补充 ε 用于终止递归。" in brief.read_text(encoding="utf-8")
    assert "补充 ε" not in (workspace / "lesson.json").read_text(encoding="utf-8")
    before = detailed.read_bytes(), brief.read_bytes()
    client.chat.return_value = json.dumps({"reply": "ε 允许结束递归。", "lesson": None})
    reply = continue_notes(workspace, "ε 有什么用？", [], detailed, LlmConfig(), AgentConfig(), client=client)
    assert not reply.updated
    assert before == (detailed.read_bytes(), brief.read_bytes())

    data["examples"][0]["figure_ids"] = ["invented"]
    client.chat.return_value = json.dumps({"reply": "已修改。", "lesson": data})
    with pytest.raises(LlmError, match="未知画面"):
        continue_notes(workspace, "修改例题", [], detailed, LlmConfig(), AgentConfig(), client=client)
    assert before == (detailed.read_bytes(), brief.read_bytes())

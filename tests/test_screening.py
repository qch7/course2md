"""两段式读图：缩略图总览初筛后只读入选画面的原图。"""

from __future__ import annotations

import json
import re
from unittest.mock import Mock

import pytest
from PIL import Image

from course2md.agents import AgentConfig
from course2md.config import LlmConfig
from course2md.frames.sampling import Frame
from course2md.refine.vision.screening import COLUMNS, TILE_SIZE, Decision, build_sheet, parse_decisions
from course2md.refine.vision.visuals import read_visuals


@pytest.fixture
def figures(tmp_path):
    result = {}
    for index in range(1, 7):
        path = tmp_path / f"frame-{index}.jpg"
        Image.new("RGB", (1920, 1080), (index * 30, 100, 200 - index * 20)).save(path, format="JPEG")
        result[f"f{index}"] = Frame(index * 60, path)
    return result


def _images_in(messages) -> int:
    return sum(1 for part in messages[0]["content"] if part["type"] == "image_url")


def _screen(*items):
    return json.dumps(
        {
            "figures": [
                {"figure_id": key, "decision": decision, "same_as": same_as, "note": "依据"}
                for key, decision, same_as in items
            ]
        }
    )


def _read(messages):
    ids = re.findall(r"^(f\d+) \[", messages[0]["content"][0]["text"], re.M)
    return json.dumps({"pages": [{"figure_id": key, "status": "readable", "content": f"{key} 的课件"} for key in ids]})


class Model:
    """初筛请求返回预设结论，原图请求逐张返回内容，并记录每次请求的图像 ID。"""

    def __init__(self, *screen_replies: str) -> None:
        self.screen_replies = list(screen_replies)
        self.screen_calls: list[int] = []
        self.read_calls: list[list[str]] = []

    def __call__(self, messages):
        text = messages[0]["content"][0]["text"]
        if "缩略图总览" in text:
            self.screen_calls.append(_images_in(messages))
            return self.screen_replies.pop(0)
        self.read_calls.append(re.findall(r"^(f\d+) \[", text, re.M))
        return _read(messages)


def test_screening_reads_only_selected_originals(figures, tmp_path):
    model = Model(
        _screen(
            ("f1", "read", ""),
            ("f2", "duplicate", "f1"),
            ("f3", "skip", ""),
            ("f4", "read", ""),
            # f5 未返回结论 → 读原图；f6 指向被跳过的画面 → 读原图
            ("f6", "duplicate", "f3"),
        )
    )
    client = Mock(chat=Mock(side_effect=model))
    workspace = tmp_path / "work"
    cfg = LlmConfig(vision=True, max_images=12, screen_threshold=3)
    pages, calls = read_visuals(figures, cfg, AgentConfig(), workspace, Mock(), client=client)

    assert model.screen_calls == [1]  # 6 张缩略图拼成 1 张总览图
    assert model.read_calls == [["f1", "f4", "f5", "f6"]]
    assert calls == 2
    by_id = {page["figure_id"]: page for page in pages}
    assert list(by_id) == list(figures)
    assert by_id["f2"]["content"] == by_id["f1"]["content"]
    assert by_id["f3"]["status"] == "irrelevant" and by_id["f3"]["content"].startswith("初筛跳过")
    report = json.loads((workspace / "vision-coverage.json").read_text(encoding="utf-8"))
    assert report["complete"] and report["images_submitted"] == 4
    assert report["screening"] == {"active": True, "threshold": 3, "model_calls": 1, "skipped": 1, "duplicates": 1}
    sheet = next(workspace.rglob("sheet-1.jpg"))
    with Image.open(sheet) as image:
        assert image.width == TILE_SIZE[0] * COLUMNS


def test_screening_below_threshold_reads_everything(figures, tmp_path):
    model = Model()
    client = Mock(chat=Mock(side_effect=model))
    cfg = LlmConfig(vision=True, screen_threshold=6)
    read_visuals(figures, cfg, AgentConfig(), tmp_path / "work", Mock(), client=client)
    assert model.screen_calls == []
    assert model.read_calls == [list(figures)]


def test_invalid_screening_falls_back_to_reading_all(figures, tmp_path):
    model = Model("not json", json.dumps({"figures": [{"figure_id": "f1"}]}))
    client = Mock(chat=Mock(side_effect=model))
    cfg = LlmConfig(vision=True, screen_threshold=3)
    pages, calls = read_visuals(figures, cfg, AgentConfig(), tmp_path / "work", Mock(), client=client)
    assert len(model.screen_calls) == 2
    assert model.read_calls == [list(figures)]
    assert calls == 3 and all(page["status"] == "readable" for page in pages)


def test_screening_decisions_are_cached_only_while_screening(figures, tmp_path):
    workspace = tmp_path / "work"
    reply = _screen(*[(key, "skip" if key == "f3" else "read", "") for key in figures])
    cfg = LlmConfig(vision=True, screen_threshold=3)
    first = Model(reply)
    read_visuals(figures, cfg, AgentConfig(), workspace, Mock(), client=Mock(chat=Mock(side_effect=first)))
    assert first.read_calls == [["f1", "f2", "f4", "f5", "f6"]]

    again = Model()
    _, calls = read_visuals(figures, cfg, AgentConfig(), workspace, Mock(), client=Mock(chat=Mock(side_effect=again)))
    assert calls == 0

    # 关闭初筛后，之前被跳过的画面要读原图
    off = Model()
    disabled = LlmConfig(vision=True, screen_threshold=0)
    pages, calls = read_visuals(
        figures, disabled, AgentConfig(), workspace, Mock(), client=Mock(chat=Mock(side_effect=off))
    )
    assert off.read_calls == [["f3"]] and calls == 1
    assert next(page for page in pages if page["figure_id"] == "f3")["status"] == "readable"


def test_parse_decisions_defaults_to_read():
    data = {
        "figures": [
            {"figure_id": "a", "decision": "duplicate", "same_as": "a", "note": ""},
            {"figure_id": "b", "decision": "duplicate", "same_as": "zzz", "note": ""},
            {"figure_id": "c", "decision": "skip", "same_as": "", "note": "人像"},
            {"figure_id": "unknown", "decision": "skip", "same_as": "", "note": ""},
        ]
    }
    result = parse_decisions(data, ["a", "b", "c", "d"])
    assert [result[key].decision for key in "abcd"] == ["read", "read", "skip", "read"]
    assert result["c"] == Decision("skip", "", "人像")


def test_sheet_rejects_too_many_tiles(figures, tmp_path):
    items = [(f"x{index}", figures["f1"]) for index in range(13)]
    with pytest.raises(ValueError):
        build_sheet(items, tmp_path / "sheet.jpg")


def test_screen_prompt_keeps_blackboard_and_forbids_other_files():
    from course2md.refine.vision.screening import SCREEN_PROMPT
    from course2md.refine.vision.visuals import SCREEN_INSTRUCTION

    assert "板书" in SCREEN_PROMPT and "也选 read" in SCREEN_PROMPT
    assert "不要查找、打开或读取其他图片和文件" in SCREEN_PROMPT
    assert "do not search for or open any other image" in SCREEN_INSTRUCTION


def test_reading_batches_run_concurrently(tmp_path):
    import threading

    frames = {}
    for index in range(1, 10):
        path = tmp_path / f"f{index}.jpg"
        path.write_bytes(f"unique-{index}".encode())
        frames[f"f{index}"] = Frame(index, path)
    barrier = threading.Barrier(3, timeout=5)

    def chat(messages):
        barrier.wait()  # 三批同时在途才会放行，串行执行会超时
        return _read(messages)

    cfg = LlmConfig(vision=True, max_images=3, vision_concurrency=3)
    pages, calls = read_visuals(
        frames, cfg, AgentConfig(), tmp_path / "work", Mock(), client=Mock(chat=Mock(side_effect=chat))
    )
    assert calls == 3 and [page["figure_id"] for page in pages] == list(frames)


def test_failed_batch_stops_the_others(tmp_path):
    import threading

    from course2md.refine.llm import LlmError

    frames = {}
    for index in range(1, 7):
        path = tmp_path / f"f{index}.jpg"
        path.write_bytes(f"unique-{index}".encode())
        frames[f"f{index}"] = Frame(index, path)
    started = threading.Event()

    def chat(messages):
        if "f1 [" in messages[0]["content"][0]["text"]:
            raise LlmError("接口故障")
        started.set()
        return _read(messages)

    cfg = LlmConfig(vision=True, max_images=2, vision_concurrency=2)
    with pytest.raises(LlmError, match="接口故障"):
        read_visuals(frames, cfg, AgentConfig(), tmp_path / "work", Mock(), client=Mock(chat=Mock(side_effect=chat)))


def test_redirects_map_duplicates_to_their_representative(figures, tmp_path):
    from course2md.refine.vision.visuals import load_redirects

    figures["f6"].path.write_bytes(figures["f5"].path.read_bytes())  # 与 f5 内容完全相同
    model = Model(
        _screen(
            ("f1", "read", ""),
            ("f2", "duplicate", "f1"),
            ("f3", "skip", ""),
            ("f4", "read", ""),
            ("f5", "duplicate", "f4"),
        )
    )
    workspace = tmp_path / "work"
    read_visuals(
        figures,
        LlmConfig(vision=True, screen_threshold=3),
        AgentConfig(),
        workspace,
        Mock(),
        client=Mock(chat=Mock(side_effect=model)),
    )
    # f6 与 f5 字节相同，f5 又被判为 f4 的重复：沿链找到最终的代表画面
    assert load_redirects(workspace) == {"f2": "f1", "f5": "f4", "f6": "f4"}
    assert load_redirects(tmp_path / "missing") == {}


def test_notes_cite_each_page_once():
    from course2md.refine.lesson import Example, Lesson, Topic, apply_figure_redirects

    lesson = Lesson(
        "概述",
        [Topic("主题", "00:00:01", "说明", ["要点"], ["f2", "f1", "f3"])],
        [Example("例题", "00:00:02", "题干", ["步骤"], "答案", [], ["f5", "f4"], "")],
        [],
        ["复习"],
        [],
    )
    apply_figure_redirects(lesson, {"f2": "f1", "f5": "f4"})
    assert lesson.topics[0].figure_ids == ["f1", "f3"]
    assert lesson.examples[0].figure_ids == ["f4"]

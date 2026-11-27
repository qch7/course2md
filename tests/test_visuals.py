"""画面覆盖、补读、缓存和单次图像预算的回归检查。"""

from __future__ import annotations

import json
import re
import threading
from unittest.mock import Mock

import pytest

from course2md.agents import AgentConfig, AgentError
from course2md.config import LlmConfig
from course2md.frames.sampling import Frame
from course2md.refine.llm import LlmError
from course2md.refine.vision.visuals import compact_readings, read_visuals


@pytest.fixture
def figures(tmp_path):
    result = {}
    for index in range(1, 18):
        path = tmp_path / f"slide-{index}.jpg"
        path.write_bytes(f"unique-slide-{index}".encode())
        result[f"f{index}"] = Frame(index * 10, path)
    return result


def reading(*ids, status="readable"):
    return json.dumps(
        {"pages": [{"figure_id": key, "status": status, "content": f"{key} 的公式和题干。"} for key in ids]}
    )


def respond(messages):
    text = messages[0]["content"][0]["text"]
    return reading(*re.findall(r"^(f\d+) \[", text, re.M))


def test_all_images_read_in_bounded_batches_without_repeating_transcript(figures, tmp_path):
    client = Mock()
    client.chat.side_effect = respond
    workspace = tmp_path / "work"
    pages, calls = read_visuals(
        figures, LlmConfig(vision=True, max_images=6), AgentConfig(), workspace, Mock(), client=client
    )
    assert [page["figure_id"] for page in pages] == list(figures)
    assert calls == client.chat.call_count == 3
    assert sorted(len(call.args[0][0]["content"]) - 1 for call in client.chat.call_args_list) == [5, 6, 6]
    report = json.loads((workspace / "vision-coverage.json").read_text(encoding="utf-8"))
    assert report["complete"] and report["inspected"] == 17
    assert report["missing_ids"] == [] and report["images_submitted"] == 17
    assert len(list(workspace.rglob("response.json"))) == 3


def test_missing_image_is_retried_without_resending_completed_images(figures, tmp_path):
    subset = {key: figures[key] for key in ["f1", "f2", "f3"]}
    client = Mock()
    client.chat.side_effect = [reading("f1", "f3"), reading("f2")]
    pages, calls = read_visuals(subset, LlmConfig(vision=True), AgentConfig(), tmp_path / "work", Mock(), client=client)
    assert calls == 2 and len(pages) == 3
    second = client.chat.call_args.args[0][0]["content"]
    assert len(second) == 2
    assert "f2 [" in second[0]["text"] and "f1 [" not in second[0]["text"]


@pytest.mark.parametrize(
    "response",
    [
        "invalid JSON",
        reading("invented"),
        reading("f1", "f1"),
        reading(),
        json.dumps({"pages": [{"figure_id": "f1", "status": "readable", "content": "   "}]}),
    ],
)
def test_invalid_or_missing_readings_fail_with_incomplete_report(figures, tmp_path, response):
    client = Mock()
    client.chat.return_value = response
    workspace = tmp_path / "work"
    with pytest.raises(LlmError, match="覆盖不完整"):
        read_visuals({"f1": figures["f1"]}, LlmConfig(vision=True), AgentConfig(), workspace, Mock(), client=client)
    assert client.chat.call_count == 3
    report = json.loads((workspace / "vision-coverage.json").read_text(encoding="utf-8"))
    assert not report["complete"] and report["missing_ids"] == ["f1"]


def test_exact_duplicate_images_share_reading_and_compact_evidence(figures, tmp_path):
    figures["f2"].path.write_bytes(figures["f1"].path.read_bytes())
    client = Mock()
    client.chat.side_effect = respond
    pages, calls = read_visuals(
        figures, LlmConfig(vision=True), AgentConfig(), tmp_path / "work", Mock(), client=client
    )
    assert calls == 2 and len(pages) == 17
    assert pages[0]["content"] == pages[1]["content"]
    assert pages[1]["figure_id"] == "f2"
    grouped = json.loads(compact_readings(pages))
    assert grouped[0]["figure_ids"] == ["f1", "f2"]
    assert len(grouped) == 16
    assert sum(len(call.args[0][0]["content"]) - 1 for call in client.chat.call_args_list) == 16


def test_retry_reuses_validated_content_and_only_rereads_changed_image(figures, tmp_path):
    client = Mock()
    client.chat.side_effect = respond
    cfg = LlmConfig(vision=True)
    workspace = tmp_path / "work"
    pages, _ = read_visuals(figures, cfg, AgentConfig(), workspace, Mock(), client=client)
    client.reset_mock()
    cached, calls = read_visuals(figures, cfg, AgentConfig(), workspace, Mock(), client=client)
    assert cached == pages and calls == 0
    client.chat.assert_not_called()
    figures["f5"].path.write_bytes(b"changed screenshot")
    _, calls = read_visuals(figures, cfg, AgentConfig(), workspace, Mock(), client=client)
    assert calls == 1
    content = client.chat.call_args.args[0][0]["content"]
    assert len(content) == 2 and "f5 [" in content[0]["text"]


def test_text_only_backend_records_unavailable_visual_coverage(figures, tmp_path):
    client = Mock()
    pages, calls = read_visuals(figures, LlmConfig(), AgentConfig(), tmp_path, Mock(), client=client)
    assert pages == [] and calls == 0
    client.chat.assert_not_called()
    report = json.loads((tmp_path / "vision-coverage.json").read_text(encoding="utf-8"))
    assert not report["available"] and not report["complete"]
    assert len(report["missing_ids"]) == 17


def test_cached_visual_evidence_is_available_to_text_only_backend(figures, tmp_path):
    client = Mock()
    client.chat.side_effect = respond
    pages, _ = read_visuals(figures, LlmConfig(vision=True), AgentConfig(), tmp_path, Mock(), client=client)
    client.reset_mock()
    cached, calls = read_visuals(figures, LlmConfig(), AgentConfig(), tmp_path, Mock(), client=client)
    assert cached == pages and calls == 0
    client.chat.assert_not_called()


def test_cancellation_prevents_image_calls(figures, tmp_path):
    cancel = threading.Event()
    cancel.set()
    client = Mock()
    with pytest.raises(AgentError, match="取消"):
        read_visuals(figures, LlmConfig(vision=True), AgentConfig(), tmp_path, Mock(), cancel, client)
    client.chat.assert_not_called()


def test_missing_image_file_fails_before_model_call(figures, tmp_path):
    figures["f1"].path.unlink()
    client = Mock()
    with pytest.raises(LlmError, match="文件不存在"):
        read_visuals(figures, LlmConfig(vision=True), AgentConfig(), tmp_path, Mock(), client=client)
    client.chat.assert_not_called()


@pytest.mark.parametrize(
    "reply",
    [
        '{"pages": []}',
        '```json\n{"pages": []}\n```',
        '我已逐张查看全部附图。以下为符合 Schema 的 JSON：\n\n```json\n{"pages": []}\n```',
        '结果如下 {"pages": []} 以上。',
    ],
)
def test_json_reply_tolerates_agent_preamble(reply):
    from course2md.refine.llm import parse_json_reply

    assert parse_json_reply(reply) == {"pages": []}


def test_json_reply_without_json_raises():
    from course2md.refine.llm import parse_json_reply

    with pytest.raises(json.JSONDecodeError):
        parse_json_reply("没有结果")

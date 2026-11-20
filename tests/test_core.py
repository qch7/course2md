"""核心单元测试：过滤、格式、切块、本地来源。"""

from __future__ import annotations

import json

import pytest

from course2md.asr.filters import is_hallucination
from course2md.asr.formats import (
    Segment,
    merge_paragraphs,
    parse_markdown,
    parse_srt,
    to_markdown,
    to_srt,
)
from course2md.refine.notes import build_chunks
from course2md.util.text import safe_name


def test_hallucination_detection():
    assert is_hallucination("请不吝点赞 订阅 转发 打赏支持明镜与点点栏目")
    assert is_hallucination("字幕志愿者 杨茜茜")
    assert is_hallucination("   ")
    # 真实内容必须保留，哪怕很短
    assert not is_hallucination("所以它是什么呢")
    assert not is_hallucination("7779")


class FakeClient:
    """返回固定 JSON 差异清单的假客户端。"""

    def __init__(self, reply):
        self.reply = reply

    def chat(self, messages, **kwargs):
        return self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)


def test_proofread_applies_only_matching_corrections():
    import json

    from course2md.config import LlmConfig
    from course2md.refine.proofread import proofread

    segments = [Segment(0, 2, "对列里面放邮件"), Segment(65, 68, "用 Wreshk 抓包")]
    reply = json.dumps(
        [
            {"ts": "00:00:00", "old": "对列里面放邮件", "new": "队列里面放邮件"},
            {"ts": "00:01:05", "old": "用 Wreshk 抓包", "new": "用 Wireshark 抓包"},
        ],
        ensure_ascii=False,
    )
    result = proofread(segments, "测试", LlmConfig(), client=FakeClient(reply))
    assert [s.text for s in result.segments] == ["队列里面放邮件", "用 Wireshark 抓包"]
    assert result.changed == 2 and result.rejected == 0


def test_proofread_rejects_when_old_text_differs():
    import json

    from course2md.config import LlmConfig
    from course2md.refine.proofread import proofread

    segments = [Segment(0, 2, "原文一")]
    reply = json.dumps([{"ts": "00:00:00", "old": "完全不同的原文", "new": "改坏它"}], ensure_ascii=False)
    result = proofread(segments, "测试", LlmConfig(), client=FakeClient(reply))
    assert [s.text for s in result.segments] == ["原文一"]
    assert result.changed == 0 and result.rejected == 1


@pytest.mark.parametrize("old", [None, "", " ", 123])
def test_proofread_requires_original_text(old):
    from course2md.refine.proofread import apply_corrections

    segments = [Segment(0, 1, "原文")]
    fixed, changed, rejected = apply_corrections(segments, [{"ts": "00:00:00", "old": old, "new": "替换"}])
    assert fixed == segments
    assert (changed, rejected) == (0, 1)


def test_proofread_matches_different_lines_in_the_same_second():
    from course2md.refine.proofread import apply_corrections

    segments = [Segment(1.1, 1.3, "对列"), Segment(1.4, 1.8, "Wreshk")]
    fixed, changed, rejected = apply_corrections(
        segments,
        [
            {"ts": "00:00:01", "old": "Wreshk", "new": "Wireshark"},
            {"ts": "00:00:01", "old": "对列", "new": "队列"},
        ],
    )
    assert [segment.text for segment in fixed] == ["队列", "Wireshark"]
    assert [(segment.start, segment.end) for segment in fixed] == [(1.1, 1.3), (1.4, 1.8)]
    assert (changed, rejected) == (2, 0)


def test_proofread_rejects_ambiguous_and_repeated_corrections():
    from course2md.refine.proofread import apply_corrections

    segments = [Segment(0.1, 0.3, "重复"), Segment(0.4, 0.8, "重复"), Segment(1, 2, "对列")]
    correction = {"ts": "00:00:01", "old": "对列", "new": "队列"}
    fixed, changed, rejected = apply_corrections(
        segments,
        [
            {"ts": "00:00:00", "old": "重复", "new": "歧义替换"},
            correction,
            correction,
        ],
    )
    assert [segment.text for segment in fixed] == ["重复", "重复", "队列"]
    assert (changed, rejected) == (1, 2)


def test_proofread_tolerates_fenced_and_garbage_json():
    from course2md.config import LlmConfig
    from course2md.refine.proofread import parse_corrections, proofread

    assert parse_corrections('```json\n[{"ts": "00:00:00", "old": "a", "new": "b"}]\n```')
    assert parse_corrections("没有要改的") == []
    segments = [Segment(0, 2, "a")]
    result = proofread(segments, "测试", LlmConfig(), client=FakeClient("我们不需要改"))
    assert [s.text for s in result.segments] == ["a"]


def test_paragraph_merge_splits_on_sentence_end():
    segments = [
        Segment(0, 2, "第一句话"),
        Segment(2, 4, "第二句话。"),
        Segment(9, 11, "停顿之后"),
    ]
    paragraphs = merge_paragraphs(segments, gap=1.6)
    assert len(paragraphs) == 2
    assert paragraphs[0].text == "第一句话，第二句话。"
    assert paragraphs[1].start == 9


def test_srt_roundtrip():
    segments = [Segment(0.5, 2.25, "你好"), Segment(3, 4.5, "世界")]
    text = to_srt(segments)
    parsed = parse_srt(text)
    assert [s.text for s in parsed] == ["你好", "世界"]
    assert parsed[0].start == pytest.approx(0.5, abs=0.001)


def test_markdown_roundtrip():
    segments = [Segment(65, 70, "第一段"), Segment(70, 75, "第二段")]
    md = to_markdown(segments, "标题", {"课程": "计算机网络"})
    parsed = parse_markdown(md)
    assert len(parsed) == 1
    assert parsed[0].start == 65


def test_chunks_cover_all_paragraphs():
    paragraphs = merge_paragraphs([Segment(i * 5, i * 5 + 4, f"这是第{i}句课堂内容。") for i in range(40)])
    chunks = build_chunks(paragraphs, chunk_chars=200)
    assert len(chunks) > 1
    assert chunks[0].index == 1 and chunks[-1].index == len(chunks)
    assert all(c.body.strip() for c in chunks)
    assert chunks[0].start == paragraphs[0].start
    assert chunks[-1].end == paragraphs[-1].end


def test_safe_name_strips_illegal_chars():
    # 末尾的下划线一并去掉，避免 Windows 上带尾点的文件名
    assert safe_name('a/b:c*d?"e"') == "a_b_c_d_e"
    assert safe_name("   ") == "course"


def test_agent_config_resolution(monkeypatch):
    from course2md import agents as agent

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["C:/fake/dsh.exe", "--expose-internals", "cli.js"])
    cmd = agent.AgentConfig(preset="dsh").resolved_command()
    assert cmd[:1] == ["C:/fake/dsh.exe"] and cmd[-2] == "headless"

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: None)
    assert agent.AgentConfig(preset="dsh").resolved_command() == []
    assert agent.AgentConfig(command=["my-agent", "run"]).resolved_command() == ["my-agent", "run"]


def test_chat_client_retries_on_truncation(monkeypatch):
    from course2md.config import LlmConfig
    from course2md.refine import llm as llm_mod

    calls = []

    class FakeResp:
        status_code = 200
        text = ""

        def __init__(self, finish, content):
            self._finish, self._content = finish, content

        def json(self):
            return {"choices": [{"message": {"content": self._content}, "finish_reason": self._finish}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json["max_tokens"])
        return FakeResp("length", "半截") if len(calls) == 1 else FakeResp("stop", "完整结果")

    monkeypatch.setattr(llm_mod.requests, "post", fake_post)
    cfg = LlmConfig(api_key="test", max_tokens=1000)
    text = llm_mod.ChatClient(cfg).chat([{"role": "user", "content": "hi"}])
    assert text == "完整结果"
    assert calls == [1000, 2000]


def test_select_changes_filters_short_gaps():
    from course2md.asr.frames import select_changes

    stamps = [0, 5, 10, 15, 20, 25, 30]
    diffs = [0, 9, 0, 50, 0, 0]
    # 10s 变化但离首帧不足 15s → 丢；25s 离上一张只有 5s → 丢
    assert select_changes(stamps, diffs, threshold=8, min_gap=15) == [0, 20]


def test_select_changes_respects_max_frames():
    from course2md.asr.frames import select_changes

    stamps = list(range(0, 200, 5))
    diffs = [50.0] * (len(stamps) - 1)
    assert len(select_changes(stamps, diffs, threshold=8, min_gap=15, max_frames=3)) == 3
    assert (
        select_changes(
            [],
            [],
        )
        == []
    )


def test_frame_index_written(tmp_path):
    from course2md.asr.frames import Frame, write_index

    frames = [Frame(226, tmp_path / "00-03-46.jpg"), Frame(690, tmp_path / "00-11-30.jpg")]
    path = write_index(frames, tmp_path / "index.md")
    text = path.read_text(encoding="utf-8")
    assert "00:03:46" in text and "00-03-46.jpg" in text


def test_week_helpers_align_to_monday_and_report_basis():
    from datetime import date

    from course2md.resolvers.week import parse_date, resolve_term_start, week_of

    assert parse_date("2026-11-09 15:15") == date(2026, 11, 9)
    assert parse_date("2026/9/7") == date(2026, 9, 7)
    assert parse_date("") is None and parse_date("时间未定") is None

    start, source = resolve_term_start("2026-09-07", None, ["2026-11-09"])
    assert (start, source) == (date(2026, 9, 7), "configured")
    assert week_of(date(2026, 11, 9), start) == 10

    start, source = resolve_term_start(None, "2026-09-01", ["2026-11-09"])
    assert (start, source) == (date(2026, 8, 31), "api")

    start, source = resolve_term_start(None, None, ["2026-11-09", "2026-10-26"])
    assert (start, source) == (date(2026, 10, 26), "inferred")
    assert week_of(date(2026, 10, 26), start) == 1
    assert resolve_term_start(None, None, [""]) == (None, "")


def test_local_sessions_group_by_folder(tmp_path):
    from course2md.resolvers.local import LocalResolver

    folder = tmp_path / "计算机网络"
    folder.mkdir()
    video = folder / "第 1 讲.mp4"
    video.write_bytes(b"")

    single = LocalResolver().list_sessions(str(video))
    assert single[0].course == "计算机网络"
    assert single[0].title == "第 1 讲"

    folder_sessions = LocalResolver().list_sessions(str(folder))
    assert [session.course for session in folder_sessions] == ["计算机网络"]

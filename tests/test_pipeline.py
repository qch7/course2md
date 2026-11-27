"""多机位输入经完整流水线保留产物并选择笔记主音轨。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from course2md import pipeline
from course2md.agents import AgentConfig
from course2md.asr.formats import Segment, parse_srt
from course2md.asr.frames import Frame
from course2md.refine.lesson import NotePair
from course2md.resolvers.base import MediaRef
from course2md.stages import Cancelled
from course2md.stages import media as media_stage
from course2md.stages.options import JobOptions


@pytest.fixture
def media_pipeline(tmp_path, monkeypatch):
    refs = [
        MediaRef(str(tmp_path / "lecture_screen.mp4"), "lecture_screen.mp4", view=5),
        MediaRef(str(tmp_path / "lecture_room.mp4"), "lecture_room.mp4", view=1),
    ]
    texts = iter(
        [
            [Segment(0, 2, "屏幕音轨。"), Segment(100, 102, "屏幕结尾。")],
            [Segment(0, 2, "教室音轨。"), Segment(100, 102, "教室结尾。")],
        ]
    )
    backend = SimpleNamespace(transcribe=lambda samples: next(texts))
    monkeypatch.setattr(media_stage, "get_resolver", lambda source: SimpleNamespace(resolve=lambda *args: refs))
    monkeypatch.setattr(media_stage, "WhisperBackend", lambda cfg: backend)
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 120))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])
    return JobOptions(source="mock", out_dir=tmp_path / "out", proofread=False, frames=False)


def assert_media_outputs(result):
    assert result.primary_media == "lecture_room"
    assert result.transcripts == result.transcripts_by_media["lecture_room"]
    assert set(result.transcripts_by_media) == {"lecture_room", "lecture_screen"}
    for stem, paths in result.transcripts_by_media.items():
        assert set(paths) == {"srt", "txt", "md"}
        assert all(path.is_file() for path in paths.values())
        segments = parse_srt(paths["srt"].read_text(encoding="utf-8"))
        assert len(segments) == 2
        assert ("教室" if stem == "lecture_room" else "屏幕") in segments[0].text
    manifest = json.loads((result.output_dir / "job.json").read_text(encoding="utf-8"))
    assert manifest["primary_media"] == "lecture_room"
    assert set(manifest["transcripts_by_media"]) == {"lecture_room", "lecture_screen"}
    assert manifest["transcripts"]["srt"].endswith("lecture_room.srt")


def test_builtin_notes_use_one_audio_timeline(media_pipeline, monkeypatch):
    from course2md.refine import lesson

    captured = {}

    def build_notes(segments, title, *args, **kwargs):
        captured.update(segments=segments, title=title)
        return NotePair("# 详细版\n", "# 缩略版\n")

    monkeypatch.setattr(lesson, "build_note_pair", build_notes)
    result = pipeline.run_job(media_pipeline)
    assert [segment.start for segment in captured["segments"]] == [0, 100]
    assert all("教室" in segment.text for segment in captured["segments"])
    assert captured["title"] == "lecture_room"
    assert result.notes == result.detailed_notes
    assert result.detailed_notes.read_text(encoding="utf-8") == "# 详细版\n"
    assert "缩略版" in result.brief_notes.read_text(encoding="utf-8")
    assert_media_outputs(result)


def test_agent_uses_primary_audio_and_keeps_frames_from_both_views(media_pipeline, monkeypatch):
    from course2md.asr import frames
    from course2md.refine import lesson

    media_pipeline.agent = AgentConfig(command=["fake-agent"])
    media_pipeline.frames = True
    media_pipeline.detail = False
    captured = {}

    def extract(media, out_dir, prefix, **kwargs):
        assert kwargs["max_frames"] is None
        path = out_dir / f"{prefix}00-00-00.jpg"
        path.write_bytes(media.stem.encode())
        return [Frame(0, path)]

    def build_notes(segments, title, *args, frames, **kwargs):
        captured.update(segments=segments, frames=frames)
        return NotePair("# 详细版\n", "# 缩略版\n")

    monkeypatch.setattr(frames, "extract", extract)
    monkeypatch.setattr(lesson, "build_note_pair", build_notes)
    result = pipeline.run_job(media_pipeline)
    assert all("教室" in segment.text for segment in captured["segments"])
    assert {frame.path.name for frame in captured["frames"]} == {
        "lecture_room_00-00-00.jpg",
        "lecture_screen_00-00-00.jpg",
    }
    assert result.notes == result.brief_notes
    assert len(list((result.output_dir / "frames").glob("*.jpg"))) == 2
    assert_media_outputs(result)


def test_empty_audio_stops_before_note_generation(tmp_path, monkeypatch):
    ref = MediaRef(str(tmp_path / "silent.mp4"), "silent.mp4")
    monkeypatch.setattr(media_stage, "get_resolver", lambda source: SimpleNamespace(resolve=lambda *args: [ref]))
    monkeypatch.setattr(media_stage, "WhisperBackend", lambda cfg: SimpleNamespace(transcribe=lambda samples: []))
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 120))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])
    with pytest.raises(RuntimeError, match="没有识别到讲话"):
        pipeline.run_job(JobOptions(source="mock", out_dir=tmp_path / "out", frames=False))


def test_empty_room_transcript_selects_other_view(media_pipeline, monkeypatch):
    texts = iter([[Segment(0, 2, "屏幕讲话。")], []])
    monkeypatch.setattr(
        media_stage, "WhisperBackend", lambda cfg: SimpleNamespace(transcribe=lambda samples: next(texts))
    )
    media_pipeline.ai = False
    result = pipeline.run_job(media_pipeline)
    assert result.primary_media == "lecture_screen"
    assert result.transcripts["srt"].name == "lecture_screen.srt"
    assert len(result.transcripts_by_media) == 2


def test_proofread_metadata_counts_changes_for_each_view(media_pipeline, monkeypatch):
    from importlib import import_module

    from course2md.refine import lesson
    from course2md.refine.proofread import ProofreadResult

    media_pipeline.proofread = True
    monkeypatch.setattr(
        import_module("course2md.refine.proofread"),
        "proofread",
        lambda segments, title, *args, **kwargs: ProofreadResult(segments, 1 if title == "lecture_screen" else 2),
    )
    monkeypatch.setattr(lesson, "build_note_pair", lambda *args, **kwargs: NotePair("# 详细版\n", "# 缩略版\n"))
    result = pipeline.run_job(media_pipeline)
    assert result.corrected == 3
    for stem, count in (("lecture_room", 2), ("lecture_screen", 1)):
        meta = json.loads((result.output_dir / f"{stem}.meta.json").read_text(encoding="utf-8"))
        assert meta["校对"] == f"AI 校对（{count} 处）"


@pytest.mark.parametrize("outcome", ["done", "failed", "cancelled"])
def test_download_cleanup_waits_for_successful_outputs(media_pipeline, monkeypatch, outcome):
    import threading

    from course2md.refine import lesson
    from course2md.refine.llm import LlmError

    media_pipeline.delete_video = True
    ref = MediaRef("https://example.com/video", "download.mp4", view=1)
    monkeypatch.setattr(media_stage, "get_resolver", lambda source: SimpleNamespace(resolve=lambda *args: [ref]))
    monkeypatch.setattr(media_stage, "download", lambda ref, dest, **kwargs: dest.write_bytes(b"download"))
    cancel = threading.Event()

    def build(*args, **kwargs):
        if outcome == "failed":
            raise LlmError("生成失败")
        if outcome == "cancelled":
            cancel.set()
        return NotePair("# 详细版\n", "# 缩略版\n")

    monkeypatch.setattr(lesson, "build_note_pair", build)
    if outcome == "done":
        result = pipeline.run_job(media_pipeline, cancel=cancel)
        assert len(result.deleted_media) == 1
        assert result.detailed_notes.is_file() and result.brief_notes.is_file()
        assert not (result.output_dir / "media" / "download.mp4").exists()
    else:
        with pytest.raises((LlmError, Cancelled)):
            pipeline.run_job(media_pipeline, cancel=cancel)
        assert (media_pipeline.out_dir / "media" / "download.mp4").read_bytes() == b"download"


def test_cleanup_keeps_imported_local_video(media_pipeline, monkeypatch):
    from course2md.refine import lesson

    local = media_pipeline.out_dir.parent / "lecture_room.mp4"
    local.write_bytes(b"user-video")
    media_pipeline.delete_video = True
    monkeypatch.setattr(lesson, "build_note_pair", lambda *args, **kwargs: NotePair("# 详细版\n", "# 缩略版\n"))
    result = pipeline.run_job(media_pipeline)
    assert result.deleted_media == []
    assert local.read_bytes() == b"user-video"

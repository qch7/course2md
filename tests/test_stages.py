"""阶段拆分：产物格式、执行器接口与阶段顺序。"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from course2md import pipeline
from course2md.asr.formats import Segment
from course2md.asr.frames import Frame
from course2md.refine.lesson import NotePair
from course2md.resolvers.base import MediaRef
from course2md.stages import (
    Cancelled,
    FrameSet,
    LocalExecutor,
    MediaFile,
    Notes,
    StageContext,
    Transcript,
    TranscriptFiles,
    load_artifact,
    select_primary,
)
from course2md.stages import media as media_stage


def _media(**kwargs) -> MediaFile:
    return MediaFile(
        path=Path("media") / "a.mp4",
        name="a.mp4",
        remote=True,
        view=2,
        primary=False,
        meta={"subject": "编译原理"},
        **kwargs,
    )


@pytest.mark.parametrize(
    "artifact",
    [
        _media(),
        Transcript(
            stem="a",
            media=_media(),
            duration=12.5,
            segments=[Segment(0, 1.5, "你好。"), Segment(2, 3, "世界。")],
            meta={"机位": "2"},
            corrected=1,
        ),
        FrameSet(stem="a", frames=[Frame(1.0, Path("frames/a_1.jpg"))], index=Path("frames/index-a.md")),
        FrameSet(stem="b"),
        TranscriptFiles(stem="a", files={"srt": Path("a.srt")}, meta_path=Path("a.meta.json")),
        Notes(detailed=Path("notes/a.notes.md"), brief=Path("notes/a.brief.md"), workspace=None),
    ],
)
def test_artifacts_round_trip_through_json(artifact):
    data = json.loads(json.dumps(artifact.to_dict(), ensure_ascii=False))
    assert load_artifact(data) == artifact
    assert type(artifact).from_dict(data) == artifact


def test_artifact_kind_and_version_are_checked():
    data = Notes(detailed=Path("d.md"), brief=Path("b.md")).to_dict()
    with pytest.raises(ValueError, match="类型不符"):
        Transcript.from_dict(data)
    with pytest.raises(ValueError, match="版本"):
        load_artifact({**data, "version": 999})
    with pytest.raises(ValueError, match="未知产物"):
        load_artifact({"kind": "video"})


def test_frame_selector_survives_queue_serialization_and_changes_cache_keys(tmp_path):
    from course2md.stages.artifacts import OrganizeInput
    from course2md.stages.codec import cache_key, options_from_dict, options_to_dict, requires_for

    legacy = options_to_dict(pipeline.JobOptions("local", tmp_path))
    legacy.pop("frame_selector")
    classroom = options_from_dict(legacy)
    assert classroom.frame_selector == "classroom"
    options = pipeline.JobOptions("local", tmp_path, frame_selector="last-two")
    payload = options_to_dict(options)
    assert options_from_dict(payload).frame_selector == "last-two"
    for stage in ("frames", "organize"):
        assert cache_key(stage, legacy, None) == cache_key(stage, options_to_dict(classroom), None)
        assert cache_key(stage, legacy, None) != cache_key(stage, payload, None)

    transcript = Transcript("a", _media(), 5, [Segment(0, 1, "课堂内容")])
    inputs = OrganizeInput(transcript, [FrameSet("a", [Frame(0, tmp_path / "a.jpg"), Frame(1, tmp_path / "b.jpg")])])
    assert requires_for("frames", options) == ["frames:decode", "frame-selector:last-two"]
    assert requires_for("organize", options, inputs) == ["llm-api", "frame-selector:last-two"]
    assert requires_for("organize", options, OrganizeInput(transcript, [])) == ["llm-api"]


def test_select_primary_prefers_marked_then_view_one():
    def item(stem, view, primary=False, text="讲话。"):
        media = MediaFile(path=Path(f"{stem}.mp4"), name=stem, view=view, primary=primary)
        return Transcript(
            stem=stem,
            media=media,
            duration=60,
            segments=[Segment(0, 1, text)] if text else [],
            meta={"机位": str(view)},
        )

    assert select_primary([item("screen", 5), item("room", 1)]).stem == "room"
    assert select_primary([item("screen", 5, primary=True), item("room", 1)]).stem == "screen"
    assert select_primary([item("screen", 5), item("room", 1, text="")]).stem == "screen"
    with pytest.raises(RuntimeError, match="没有得到任何转写"):
        select_primary([])
    with pytest.raises(RuntimeError, match="没有识别到讲话"):
        select_primary([item("room", 1, text="")])


def test_local_executor_checks_cancel_before_stage():
    ran = []
    stage = SimpleNamespace(name="x", requires=(), run=lambda ctx, inputs: ran.append(inputs))
    ctx = StageContext(options=None, cancel=threading.Event())
    LocalExecutor().run(stage, ctx, 1)
    ctx.cancel.set()
    with pytest.raises(Cancelled):
        LocalExecutor().run(stage, ctx, 2)
    assert ran == [1]


class RecordingExecutor(LocalExecutor):
    def __init__(self):
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, stage, ctx, inputs):
        self.calls.append((stage.name, tuple(type(stage).requires(ctx.options, inputs))))
        return super().run(stage, ctx, inputs)


def _fake_media(monkeypatch, tmp_path):
    refs = [MediaRef(str(tmp_path / "room.mp4"), "room.mp4", view=1)]
    monkeypatch.setattr(media_stage, "get_resolver", lambda source: SimpleNamespace(resolve=lambda *args: refs))
    monkeypatch.setattr(
        media_stage,
        "WhisperBackend",
        lambda cfg: SimpleNamespace(cfg=cfg, transcribe=lambda samples: [Segment(0, 2, "讲话。")]),
    )
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 60))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])


def test_run_job_sends_every_stage_through_executor(tmp_path, monkeypatch):
    from importlib import import_module

    from course2md.asr import frames
    from course2md.refine import lesson
    from course2md.refine.proofread import ProofreadResult

    _fake_media(monkeypatch, tmp_path)
    monkeypatch.setattr(frames, "extract", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        import_module("course2md.refine.proofread"),
        "proofread",
        lambda segments, *args, **kwargs: ProofreadResult(segments, 0),
    )
    monkeypatch.setattr(lesson, "build_note_pair", lambda *args, **kwargs: NotePair("# 详\n", "# 略\n"))
    executor = RecordingExecutor()
    result = pipeline.run_job(pipeline.JobOptions(source="mock", out_dir=tmp_path / "out"), executor=executor)
    assert [name for name, _ in executor.calls] == ["media", "frames", "proofread", "output", "organize"]
    assert all(isinstance(requires, tuple) for _, requires in executor.calls)
    assert result.detailed_notes.is_file()


def test_run_job_without_ai_only_runs_media_and_output(tmp_path, monkeypatch):
    _fake_media(monkeypatch, tmp_path)
    executor = RecordingExecutor()
    result = pipeline.run_job(pipeline.JobOptions(source="mock", out_dir=tmp_path / "out", ai=False), executor=executor)
    assert [name for name, _ in executor.calls] == ["media", "output"]
    assert result.transcripts["srt"].is_file()


def test_media_stage_reuses_matching_backend(tmp_path, monkeypatch):
    _fake_media(monkeypatch, tmp_path)
    options = pipeline.JobOptions(source="mock", out_dir=tmp_path / "out", ai=False)
    shared = SimpleNamespace(cfg=options.asr, transcribe=lambda samples: [Segment(0, 1, "复用。")])
    stage = media_stage.MediaStage(shared)
    output = stage.run(StageContext(options=options))
    assert stage.backend is shared
    assert output.transcripts[0].segments[0].text == "复用。"

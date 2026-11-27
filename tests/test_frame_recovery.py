"""原视频被清理时恢复抽帧，复用转写内容。"""

from pathlib import Path
from types import SimpleNamespace

from course2md.asr.formats import Segment
from course2md.stages.artifacts import MediaFile, Transcript
from course2md.stages.base import StageContext
from course2md.stages.frames import FramesStage
from course2md.stages.options import JobOptions


def test_frames_redownload_missing_media_without_transcribing(tmp_path, monkeypatch):
    from course2md.stages import frames

    video = tmp_path / "media" / "course.mp4"
    media = MediaFile(video, "course.mp4", remote=True, view=1)
    transcript = Transcript("course", media, 60, [Segment(0, 2, "已有转写")])
    ref = SimpleNamespace(name="course.mp4", view=1)
    monkeypatch.setattr(frames, "get_resolver", lambda _: SimpleNamespace(resolve=lambda *a: [ref]))

    def download(_ref, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"restored-video")

    def extract(path, *_args, **_kwargs):
        assert Path(path).read_bytes() == b"restored-video"
        return []

    monkeypatch.setattr(frames, "download", download)
    monkeypatch.setattr("course2md.asr.frames.extract", extract)
    result = FramesStage().run(StageContext(JobOptions("fake://course", tmp_path)), [transcript])
    assert result[0].stem == "course"
    assert transcript.segments[0].text == "已有转写"


def test_cleanup_only_removes_downloads_in_the_job_media_directory(tmp_path):
    from course2md.stages.artifacts import MediaOutput
    from course2md.stages.cleanup import CleanupStage

    paths = [tmp_path / "media" / "download.mp4", tmp_path / "import.mp4", tmp_path / "outside.mp4"]
    paths[0].parent.mkdir()
    for path in paths:
        path.write_bytes(b"video")
    media = MediaOutput(
        files=[
            MediaFile(paths[0], "download.mp4", remote=True),
            MediaFile(paths[1], "import.mp4", remote=False),
            MediaFile(paths[2], "outside.mp4", remote=True),
        ]
    )
    CleanupStage().run(StageContext(JobOptions("x", tmp_path)), media)
    assert not paths[0].exists()
    assert paths[1].exists() and paths[2].exists()

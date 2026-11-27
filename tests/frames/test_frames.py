"""真实视频采样与截图时间的回归检查。"""

from __future__ import annotations

import json
from fractions import Fraction

import numpy as np
import pytest

from course2md.config import bootstrap_runtime
from course2md.frames.sampling import _sample, extract, select_changes


@pytest.fixture
def video(tmp_path):
    bootstrap_runtime()
    import av

    path = tmp_path / "clock.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 160, 96, "yuv420p"
        stream.codec_context.gop_size = 10
        for index in range(120):
            image = np.full((96, 160, 3), index * 2, dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.fixture
def slides_video(tmp_path):
    bootstrap_runtime()
    import av

    path = tmp_path / "slides.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 640, 360, "yuv420p"
        stream.codec_context.gop_size = 10
        for index in range(120):
            page = index // 4
            image = np.full((360, 640, 3), 255, dtype=np.uint8)
            image[70:95, 20 + page * 16 : 100 + page * 16] = 0
            image[200:225, 500 - page * 15 : 580 - page * 15] = 0
            for packet in stream.encode(av.VideoFrame.from_ndarray(image, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return path


@pytest.mark.parametrize("interval", [2.0, 2.35])
def test_sample_decodes_to_requested_time_and_reports_actual_time(video, interval):
    samples = list(_sample(video, interval))
    assert len(samples) == len(np.arange(0, 12, interval))
    for index, (timestamp, frame) in enumerate(samples):
        requested = index * interval
        assert requested <= timestamp < requested + 0.11
        assert timestamp == pytest.approx(frame.time)
        assert float(frame.to_ndarray(format="rgb24").mean()) == pytest.approx(timestamp * 20, abs=6)


def test_extract_writes_distinct_frames_with_matching_labels(video, tmp_path):
    frames = extract(video, tmp_path / "frames", interval=2, threshold=1, min_gap=1)
    assert len(frames) == 6
    assert [frame.ts for frame in frames] == pytest.approx([0, 2, 4, 6, 8, 10])
    assert len({frame.path.read_bytes() for frame in frames}) == 6
    assert all(frame.path.stem == frame.label.replace(":", "-") for frame in frames)


def test_frame_limit_covers_whole_video_and_prioritizes_example_times(video, tmp_path):
    assert select_changes(list(range(100)), [20] * 99, min_gap=1, max_frames=3) == [0, 49, 99]
    frames = extract(video, tmp_path / "limited", interval=2, threshold=1, min_gap=1, max_frames=3, focus_times=[4])
    assert [frame.ts for frame in frames] == pytest.approx([0, 4, 10])


def test_general_full_scan_keeps_changes_in_plain_frames(video, tmp_path):
    out = tmp_path / "general"
    frames = extract(video, out, max_frames=None, selector_name="general")
    assert [frame.ts for frame in frames] == pytest.approx([*range(12), 11.9])
    report = json.loads((out / "selection-clock.json").read_text(encoding="utf-8"))
    assert report["selector"] == "general"
    assert "region" not in report


def test_installed_selector_runs_on_video_and_saved_screenshots(video, tmp_path, monkeypatch):
    from course2md.frames.selection import select_frames
    from course2md.worker.capabilities import detect_capabilities

    dist = tmp_path / "course2md_demo-0.1.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Name: course2md-demo\nVersion: 0.1\n", encoding="utf-8")
    (dist / "entry_points.txt").write_text(
        "[course2md.frame_selectors]\nlast-two = demo_frame_plugin:LastTwo\n", encoding="utf-8"
    )
    (tmp_path / "demo_frame_plugin.py").write_text(
        """class LastTwo:
    def __init__(self, options):
        self.keys = []

    def add(self, key, gray):
        assert gray.ndim == 2 and gray.dtype.name == 'uint8'
        self.keys.append(key)

    def selected(self):
        return self.keys[-2:]

    def report(self):
        return {'rule': 'last-two'}
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    assert "frame-selector:last-two" in detect_capabilities(agents=False)
    out = tmp_path / "external"
    frames = extract(video, out, max_frames=None, selector_name="last-two")
    assert [frame.ts for frame in frames] == pytest.approx([11, 11.9])
    report = json.loads((out / "selection-clock.json").read_text(encoding="utf-8"))
    assert report["rule"] == "last-two"
    filtered, report = select_frames(frames, selector_name="last-two")
    assert filtered == frames
    assert list(report["sources"].values()) == [{"rule": "last-two"}]


def test_unlimited_extraction_scans_each_second_without_min_gap(slides_video, tmp_path):
    frames = extract(slides_video, tmp_path / "all", interval=5, max_frames=None)
    assert [frame.ts for frame in frames] == pytest.approx([*range(12), 11.9])
    assert all(frame.path.is_file() for frame in frames)


def test_subsecond_screenshots_have_unique_paths(slides_video, tmp_path):
    frames = extract(slides_video, tmp_path / "rapid", interval=0.4, max_frames=None)
    assert len(frames) == 30
    assert len({frame.path for frame in frames}) == 30
    assert len(list((tmp_path / "rapid").glob("*.jpg"))) == 30


def test_coverage_mode_rejects_screenshot_save_failures(slides_video, tmp_path, monkeypatch):
    from course2md.frames import sampling as frames

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(frames, "_save", fail)
    with pytest.raises(RuntimeError, match="保存失败"):
        extract(slides_video, tmp_path / "failed", max_frames=None)


def test_local_slide_changes_are_retained_even_with_long_min_gap(tmp_path):
    bootstrap_runtime()
    import av

    path = tmp_path / "small-change.mp4"
    with av.open(str(path), "w") as container:
        stream = container.add_stream("mpeg4", rate=10)
        stream.width, stream.height, stream.pix_fmt = 320, 180, "yuv420p"
        stream.codec_context.gop_size = 10
        for index in range(20):
            image = np.full((180, 320, 3), 255, dtype=np.uint8)
            if index >= 10:
                image[20:32, 20:32] = 0
            for packet in stream.encode(av.VideoFrame.from_ndarray(image, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    frames = extract(path, tmp_path / "screens", min_gap=100, max_frames=None)
    assert len(frames) == 1
    assert frames[0].ts >= 1


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_sample_rejects_nonpositive_or_nonfinite_intervals(tmp_path, interval):
    with pytest.raises(ValueError, match="采样间隔"):
        list(_sample(tmp_path / "unused.mp4", interval))


def test_sample_aligns_delayed_video_to_audio_origin(tmp_path):
    bootstrap_runtime()
    import av

    path = tmp_path / "delayed.mkv"
    with av.open(str(path), "w") as container:
        video = container.add_stream("mpeg4", rate=10)
        video.width, video.height, video.pix_fmt = 160, 96, "yuv420p"
        video.codec_context.gop_size = 10
        audio = container.add_stream("pcm_s16le", rate=16000)
        audio.layout = "mono"
        for second in range(8):
            sound = av.AudioFrame.from_ndarray(np.zeros((1, 16000), dtype=np.int16), format="s16", layout="mono")
            sound.sample_rate = 16000
            sound.pts, sound.time_base = second * 16000, Fraction(1, 16000)
            for packet in audio.encode(sound):
                container.mux(packet)
            if second < 2 or second >= 6:
                continue
            for index in range(second * 10, second * 10 + 10):
                image = np.full((96, 160, 3), index * 3, dtype=np.uint8)
                frame = av.VideoFrame.from_ndarray(image, format="rgb24")
                frame.pts, frame.time_base = index, Fraction(1, 10)
                for packet in video.encode(frame):
                    container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)
    samples = list(_sample(path, 1))
    assert [timestamp for timestamp, frame in samples] == pytest.approx([2, 3, 4, 5])
    assert all(timestamp == pytest.approx(frame.time) for timestamp, frame in samples)
    coverage = list(_sample(path, 1, strict=True))
    assert [timestamp for timestamp, frame in coverage] == pytest.approx([2, 3, 4, 5, 5.9])


@pytest.mark.parametrize("interval,strict", [(0.3, True), (1.0, True), (2.35, False), (0.5, False)])
def test_sequential_scan_matches_per_target_seeking(video, monkeypatch, interval, strict):
    """顺序解码与逐点定位取到的帧时间和画面一致。"""
    from course2md.frames import sampling as frames_module

    sequential = [(ts, frame.to_ndarray().mean()) for ts, frame in _sample(video, interval, strict=strict)]
    monkeypatch.setattr(frames_module, "SEEK_MIN_GAP", 0.0)
    monkeypatch.setattr(frames_module, "_decode_sequential", frames_module._decode_seeking)
    seeking = [(ts, frame.to_ndarray().mean()) for ts, frame in _sample(video, interval, strict=strict)]
    assert sequential == seeking and sequential


def test_explicit_timestamps_use_same_frames_either_way(video, monkeypatch):
    from course2md.frames import sampling as frames_module

    wanted = [0.0, 3.3, 7.05, 11.9]
    dense = [ts for ts, _frame in _sample(video, 1.0, wanted, strict=True)]
    monkeypatch.setattr(frames_module, "SEEK_MIN_GAP", 0.0)
    sparse = [ts for ts, _frame in _sample(video, 1.0, wanted, strict=True)]
    assert dense == sparse == pytest.approx([0.0, 3.3, 7.1, 11.9])

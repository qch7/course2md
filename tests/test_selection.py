"""本地课件筛选避免将时间戳、教室活动与动画中间状态交给模型。"""

from __future__ import annotations

import cv2
import numpy as np
import pytest
from PIL import Image

from course2md.asr.frames import Frame, extract
from course2md.asr.selection import ContentSelector, select_frames
from course2md.config import bootstrap_runtime


def classroom(text="S -> a", *, extra=False, clock=0, teacher=False):
    image = np.full((360, 640), 125, dtype=np.uint8)
    image[:220, 384:] = 30
    image[:220, :384] = 215
    cv2.putText(image, "Grammar", (15, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 40, 2)
    cv2.putText(image, text, (20, 95), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 40, 2)
    cv2.putText(image, f"12:30:{clock:02d}", (435, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 240, 1)
    if extra:
        cv2.putText(image, "E -> E+T | T", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.7, 40, 2)
    if teacher:
        cv2.rectangle(image, (295, 135), (360, 290), 35, -1)
        cv2.circle(image, (325, 130), 22, 35, -1)
    return image


def test_clock_and_teacher_motion_share_one_clear_representative():
    selector = ContentSelector()
    selector.add(0, classroom())
    for timestamp in range(1, 20):
        selector.add(timestamp, classroom(clock=timestamp, teacher=timestamp % 2 == 0))
    selected = selector.selected()
    assert len(selected) == 1
    assert selected[0] % 2 != 0 or selected[0] == 0
    assert selector.region[2] < 0.7


def test_complete_slide_covers_earlier_animation_state():
    selector = ContentSelector()
    selector.add(0, classroom())
    selector.add(10, classroom(extra=True))
    assert selector.selected() == [10]
    assert selector.selected() == [10]


def test_different_formula_is_preserved():
    selector = ContentSelector()
    selector.add(0, classroom("S -> a"))
    selector.add(10, classroom("S -> b"))
    assert selector.selected() == [0, 10]


def test_blank_frames_do_not_require_model_reading():
    selector = ContentSelector()
    for timestamp in range(20):
        selector.add(timestamp, np.full((360, 640), 100 + timestamp, np.uint8))
    assert selector.selected() == []


def test_full_screen_slides_preserve_header_and_bottom_formula():
    first = np.full((360, 640), 255, np.uint8)
    cv2.putText(first, "Chapter A", (20, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    cv2.putText(first, "E -> a", (20, 350), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    second = first.copy()
    second[325:] = 255
    cv2.putText(second, "E -> b", (20, 350), cv2.FONT_HERSHEY_SIMPLEX, 0.8, 0, 2)
    selector = ContentSelector()
    selector.add(0, first)
    selector.add(1, second)
    assert selector.selected() == [0, 1]


def test_content_deduplication_keeps_independent_views_separate(tmp_path):
    first = tmp_path / "screen_00-00-00-000.jpg"
    second = tmp_path / "room_00-00-00-000.jpg"
    Image.fromarray(classroom()).save(first)
    Image.fromarray(classroom()).save(second)
    frames, report = select_frames([Frame(0, first), Frame(0, second)])
    assert len(frames) == 2
    assert len(report["sources"]) == 2


def test_local_filtering_observes_cancellation(tmp_path):
    def cancel():
        raise InterruptedError("任务已取消")

    with pytest.raises(InterruptedError, match="取消"):
        select_frames([Frame(0, tmp_path / "one.jpg"), Frame(1, tmp_path / "two.jpg")], check_cancel=cancel)


def test_existing_screenshots_are_filtered_before_model_input(tmp_path):
    frames = []
    for timestamp in range(12):
        path = tmp_path / f"room_00-00-{timestamp:02d}-000.jpg"
        Image.fromarray(classroom(clock=timestamp, teacher=timestamp % 3 == 0)).save(path)
        frames.append(Frame(timestamp, path))
    selected, report = select_frames(frames)
    assert report["input_count"] == 12 and report["selected_count"] == 1
    assert len(selected) == 1
    assert all(frame.path.is_file() for frame in frames)


def test_extraction_only_saves_content_representatives(tmp_path):
    bootstrap_runtime()
    import av

    video = tmp_path / "classroom.mp4"
    with av.open(str(video), "w") as container:
        stream = container.add_stream("mpeg4", rate=2)
        stream.width, stream.height, stream.pix_fmt = 640, 360, "yuv420p"
        for index in range(20):
            gray = classroom(clock=index, teacher=index % 3 == 0)
            rgb = np.repeat(gray[:, :, None], 3, axis=2)
            for packet in stream.encode(av.VideoFrame.from_ndarray(rgb, format="rgb24")):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    frames = extract(video, tmp_path / "frames", max_frames=None)
    # 编码噪声可能留下一个近似状态，保守保留以免吞掉公式变化。
    assert 1 <= len(frames) <= 2
    assert len(list((tmp_path / "frames").glob("*.jpg"))) == len(frames)


def test_checkin_qr_screen_is_filtered_locally():
    encoded = cv2.QRCodeEncoder_create().encode("https://example.com/checkin")
    qr = cv2.resize(encoded, (240, 240), interpolation=cv2.INTER_NEAREST)
    image = np.full((360, 640), 255, np.uint8)
    image[50:290, 200:440] = qr
    selector = ContentSelector()
    selector.add(0, image)
    assert selector.selected() == []
    assert selector.groups[0].signature.reason == "qr"

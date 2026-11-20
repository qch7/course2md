"""按时间采样视频，交给筛选器选图，再解码保存高清截图。

首轮采样通过本地内容特征筛选代表图，选定后再解码保存高清截图。
截图时间与音轨对齐，记录实际取得的帧时间，供多模态整理与 Markdown 配图使用。
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..frame_selectors.api import CHANGE_THRESHOLD, DEFAULT_SELECTOR, MAX_FRAMES, MIN_GAP_SECONDS, SelectionOptions
from ..frame_selectors.general import select_changes
from ..frame_selectors.registry import create_selector, selected_keys
from ..util.text import hhmmss

__all__ = [
    "CHANGE_THRESHOLD",
    "FRAME_WIDTH",
    "MAX_FRAMES",
    "MIN_GAP_SECONDS",
    "SAMPLE_SECONDS",
    "Frame",
    "Progress",
    "extract",
    "select_changes",
    "write_index",
]

Progress = Callable[[float, str], None]

SAMPLE_SECONDS = 5.0
FRAME_WIDTH = 1920
SEEK_MIN_GAP = 10.0  # 平均目标间隔超过该秒数时逐点定位，否则顺序解码


@dataclass
class Frame:
    """一帧关键画面，label 与转写稿时间戳格式一致，便于对齐。"""

    ts: float
    path: Path

    @property
    def label(self) -> str:
        return hhmmss(self.ts)


def _noop(_fraction: float, _message: str) -> None:
    return None


def _save(frame, target: Path, width: int) -> None:
    from PIL import Image

    image: Image.Image = frame.to_image()
    if image.width > width:
        image = image.resize((width, round(image.height * width / image.width)))
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target, format="JPEG", quality=80, optimize=True)


def _sample(
    media: Path, interval: float, timestamps: list[float] | None = None, *, strict: bool = False
) -> Iterator[tuple[float, Any]]:
    """按正数 interval 秒采样，时间相对首条音轨起点，无音轨时相对视频起点。"""
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("画面采样间隔必须为大于 0 的有限秒数")
    if timestamps == []:
        return
    import av

    container = av.open(str(media))
    try:
        if not container.streams.video:
            return
        stream = container.streams.video[0]
        video_start = float((stream.start_time or 0) * stream.time_base)
        origin = video_start
        if container.streams.audio:
            audio = container.streams.audio[0]
            origin = float((audio.start_time or 0) * audio.time_base)
        duration = (
            video_start + float(stream.duration * stream.time_base) - origin
            if stream.duration is not None
            else (container.duration or 0) / av.time_base
        )
        start = max(0.0, video_start - origin)
        targets = list(np.arange(start, duration, interval)) if timestamps is None else sorted(set(timestamps))
        if strict and timestamps is None and duration > start:
            targets.append(duration - 0.000001)
        allow_end = strict and timestamps is None
        span = max(0.0, duration - start)
        # 目标稠密（全课扫描或平均间隔不超过 SEEK_MIN_GAP 秒）时顺序解码一遍，
        # 避免每个时间点都从关键帧重新解码；稀疏目标逐点定位更快。
        sequential = timestamps is None or len(targets) * SEEK_MIN_GAP >= span
        reader = (
            _decode_sequential(container, stream, origin, targets, strict, allow_end)
            if sequential
            else _decode_seeking(container, stream, origin, targets, strict, allow_end)
        )
        last_timestamp = None
        for frame, reached_end in reader:
            timestamp = float(frame.time) - origin
            if last_timestamp is not None and timestamp <= last_timestamp:
                if reached_end:
                    break
                continue
            last_timestamp = timestamp
            yield timestamp, frame
            if reached_end:
                break
    finally:
        container.close()


def _decode_sequential(
    container, stream, origin: float, targets: list[float], strict: bool, allow_end: bool
) -> Iterator[tuple[Any, bool]]:
    """顺序解码，每个目标取首个不早于它的帧；到达结尾后按需返回最后一帧。"""
    import av

    index = 0
    last = None
    try:
        for decoded in container.decode(stream):
            if index >= len(targets):
                return
            if decoded.time is None:
                continue
            last = decoded
            while index < len(targets) and decoded.time >= origin + targets[index] - 0.000001:
                index += 1
                yield decoded, False
    except av.error.FFmpegError as exc:
        if strict:
            target = targets[min(index, len(targets) - 1)]
            raise RuntimeError(f"无法读取画面 {hhmmss(target)}，画面扫描不完整") from exc
        return
    if index < len(targets):
        if allow_end and last is not None:
            yield last, True
        elif strict:
            raise RuntimeError(f"无法读取画面 {hhmmss(targets[index])}，画面扫描不完整")


def _decode_seeking(
    container, stream, origin: float, targets: list[float], strict: bool, allow_end: bool
) -> Iterator[tuple[Any, bool]]:
    """逐个目标定位到前一个关键帧后解码，适合稀疏的时间点。"""
    import av

    for ts in targets:
        reached_end = False
        try:
            container.seek(int((origin + ts) / stream.time_base), backward=True, stream=stream)
            frame = None
            for decoded in container.decode(stream):
                if decoded.time is None:
                    continue
                frame = decoded
                if decoded.time >= origin + ts - 0.000001:
                    break
            else:
                if not (allow_end and frame is not None):
                    raise StopIteration  # noqa: PLR1708  由下方 except 统一处理
                reached_end = True
            if frame is None:
                raise StopIteration  # noqa: PLR1708
        except (StopIteration, av.error.FFmpegError) as exc:
            if strict:
                raise RuntimeError(f"无法读取画面 {hhmmss(ts)}，画面扫描不完整") from exc
            continue
        yield frame, reached_end
        if reached_end:
            return


def extract(
    media: Path,
    out_dir: Path,
    interval: float = SAMPLE_SECONDS,
    threshold: float = CHANGE_THRESHOLD,
    min_gap: float | None = None,
    width: int = FRAME_WIDTH,
    max_frames: int | None = MAX_FRAMES,
    prefix: str = "",
    progress: Progress = _noop,
    focus_times: list[float] | None = None,
    check_cancel: Callable[[], None] = lambda: None,
    *,
    selector_name: str | None = None,
) -> list[Frame]:
    """扫描后保存截图。max_frames=None 时密集扫描并检查画面覆盖。"""
    if max_frames is not None and max_frames < 1:
        raise ValueError("关键帧数量必须大于 0")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("画面采样间隔必须为大于 0 的有限秒数")
    full_scan = max_frames is None
    scan_interval = min(interval, 1.0) if full_scan else interval
    strategy = selector_name if selector_name is not None else (DEFAULT_SELECTOR if full_scan else "general")
    selector = create_selector(
        strategy,
        SelectionOptions(
            threshold=threshold,
            min_gap=(0.0 if full_scan else MIN_GAP_SECONDS) if min_gap is None else min_gap,
            max_frames=max_frames,
            focus_times=tuple(focus_times or ()),
        ),
    )
    stamps: list[float] = []
    for ts, frame in _sample(media, scan_interval, strict=full_scan):
        check_cancel()
        stamps.append(ts)
        gray = frame.reformat(width=640, height=360, format="gray").to_ndarray()
        selector.add(ts, gray)
        if len(stamps) % 40 == 0:
            progress(0.5, f"扫描画面 {hhmmss(ts)}")
    if not stamps:
        return []
    check_cancel()
    selected = selected_keys(selector, stamps)
    if max_frames is not None and len(selected) > max_frames:
        selected = [selected[index] for index in np.linspace(0, len(selected) - 1, max_frames, dtype=int)]
    if full_scan or selector_name is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        report_path = out_dir / f"selection-{prefix.rstrip('_') or media.stem}.json"
        report_path.write_text(
            json.dumps(
                {
                    **selector.report(),
                    "selector": strategy,
                    "input_count": len(stamps),
                    "selected_count": len(selected),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    frames: list[Frame] = []
    for ts, frame in _sample(media, scan_interval, selected, strict=full_scan):
        check_cancel()
        stamp = hhmmss(ts).replace(":", "-")
        if full_scan:
            stamp += f"-{round((ts % 1) * 1000):03d}"
        target = out_dir / f"{prefix}{stamp}.jpg"
        try:
            _save(frame, target, width)
        except Exception as exc:
            if full_scan:
                raise RuntimeError(f"画面 {hhmmss(ts)} 保存失败，无法完成画面覆盖") from exc
            continue
        frames.append(Frame(ts, target))
        progress(0.8 + 0.2 * len(frames) / len(selected), f"保存关键帧 {len(frames)}/{len(selected)}")
    if full_scan and len(frames) != len(selected):
        raise RuntimeError(f"画面提取不完整，应保存 {len(selected)} 张，实际 {len(frames)} 张")
    progress(1.0, f"挑选关键帧 {len(frames)} 张（采样 {len(stamps)} 次）")
    return frames


def write_index(frames: list[Frame], target: Path, title: str = "画面索引") -> Path:
    """写出纯文本索引：时间 → 绝对路径。"""
    lines = [f"# {title}", "", "| 时间 | 截图 |", "| --- | --- |"]
    for frame in frames:
        lines.append(f"| {frame.label} | `{frame.path}` |")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target

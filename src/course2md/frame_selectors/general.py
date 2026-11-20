"""基于整幅灰度图帧差的通用筛选。"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .api import CHANGE_THRESHOLD, MAX_FRAMES, MIN_GAP_SECONDS, SelectionOptions


def select_changes(
    stamps: list[float],
    diffs: list[float],
    threshold: float = CHANGE_THRESHOLD,
    min_gap: float = MIN_GAP_SECONDS,
    max_frames: int | None = MAX_FRAMES,
) -> list[float]:
    """保留首帧与相邻帧差超过阈值的时间点，超出上限时均匀抽取。"""
    if not stamps:
        return []
    kept = [stamps[0]]
    for ts, diff in zip(stamps[1:], diffs, strict=False):
        if diff >= threshold and ts - kept[-1] >= min_gap:
            kept.append(ts)
    if max_frames is not None and len(kept) > max_frames:
        return [kept[index] for index in np.linspace(0, len(kept) - 1, max_frames, dtype=int)]
    return kept


class ChangeSelector:
    label = "通用画面"
    description = "按整幅画面的变化选帧，适合屏幕录制、网课与非投影场景"

    def __init__(self, options: SelectionOptions) -> None:
        self.options = options
        self.stamps: list[float] = []
        self.diffs: list[float] = []
        self.last_thumb: np.ndarray | None = None

    def add(self, key: float, gray: np.ndarray) -> None:
        thumb = np.asarray(Image.fromarray(gray).resize((320, 180), Image.Resampling.BILINEAR), dtype=np.int16)
        if self.last_thumb is not None:
            self.diffs.append(float(np.abs(thumb - self.last_thumb).mean()))
        self.stamps.append(key)
        self.last_thumb = thumb

    def selected(self) -> list[float]:
        if not self.stamps:
            return []
        options = self.options
        limit = options.max_frames
        candidates = select_changes(self.stamps, self.diffs, options.threshold, options.min_gap, limit)
        focused = sorted({min(self.stamps, key=lambda ts: abs(ts - target)) for target in options.focus_times})
        if limit is None:
            return sorted(set(candidates) | set(focused))
        if len(focused) >= limit:
            return [focused[index] for index in np.linspace(0, len(focused) - 1, limit, dtype=int)]
        selected = set(focused or candidates[:1])
        remaining = set(candidates) - selected
        while len(selected) < limit and remaining:
            candidate = max(sorted(remaining), key=lambda ts: min(abs(ts - chosen) for chosen in selected))
            selected.add(candidate)
            remaining.remove(candidate)
        return sorted(selected)

    def report(self) -> dict:
        return {
            "threshold": self.options.threshold,
            "min_gap": self.options.min_gap,
            "max_frames": self.options.max_frames,
        }

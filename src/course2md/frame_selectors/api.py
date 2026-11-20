"""画面筛选插件的公开接口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np

DEFAULT_SELECTOR = "classroom"
CHANGE_THRESHOLD = 8.0
MIN_GAP_SECONDS = 8.0
MAX_FRAMES = 40


@dataclass(frozen=True)
class SelectionOptions:
    """可选的筛选参数。focus_times 与视频时间使用同一个时间基准。"""

    threshold: float = CHANGE_THRESHOLD
    min_gap: float = MIN_GAP_SECONDS
    max_frames: int | None = MAX_FRAMES
    focus_times: tuple[float, ...] = ()


@runtime_checkable
class FrameSelector(Protocol):
    """每个视频或同组截图使用独立实例，由 ``factory(options)`` 创建。"""

    def add(self, key: float, gray: np.ndarray) -> None:
        """按顺序接收二维 uint8 灰度图。key 是视频时间（秒）或该图的输入序号。"""
        ...

    def selected(self) -> list[float]:
        """返回应该保留的输入 key，不得返回新 key。"""
        ...

    def report(self) -> dict:
        """返回可序列化为 JSON 的筛选依据。"""
        ...

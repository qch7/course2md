"""对已有截图分组并应用选定的画面筛选策略。"""

from __future__ import annotations

import re
from collections.abc import Callable

import numpy as np
from PIL import Image

from ..frame_selectors.api import DEFAULT_SELECTOR, FrameSelector, SelectionOptions
from ..frame_selectors.registry import create_selector, selected_keys
from .frames import Frame


def __getattr__(name: str):
    if name == "ContentSelector":
        from ..frame_selectors.classroom import ContentSelector

        return ContentSelector
    raise AttributeError(name)


def select_frames(
    frames: list[Frame],
    progress: Callable[[float, str], None] = lambda f, m: None,
    check_cancel: Callable[[], None] = lambda: None,
    *,
    selector_name: str = DEFAULT_SELECTOR,
) -> tuple[list[Frame], dict]:
    """按来源筛选已有截图，返回代表图和报告。"""
    if len(frames) < 2:
        return frames, {
            "selector": selector_name,
            "input_count": len(frames),
            "selected_count": len(frames),
            "sources": {},
        }
    # 筛选现有截图时，通用策略只合并画面近似相同的连续截图。
    options = SelectionOptions(threshold=1.0, min_gap=0.0, max_frames=None)
    selectors: dict[str, tuple[FrameSelector, list[float]]] = {}
    for index, frame in enumerate(frames):
        check_cancel()
        source = re.sub(r"\d{2}-\d{2}-\d{2}(?:-\d+)?$", "", frame.path.stem)
        # 没有时间戳的独立图片作为同一组输入，仍可合并重复内容。
        if source == frame.path.stem:
            source = str(frame.path.parent)
        if source not in selectors:
            selectors[source] = (create_selector(selector_name, options), [])
        selector, keys = selectors[source]
        with Image.open(frame.path) as image:
            gray = np.asarray(image.convert("L"))
        selector.add(index, gray)
        keys.append(index)
        if (index + 1) % 50 == 0:
            progress((index + 1) / len(frames), f"本地筛选画面 {index + 1}/{len(frames)}")
    selected_indices = sorted(
        int(key) for selector, keys in selectors.values() for key in selected_keys(selector, keys)
    )
    selected = [frames[index] for index in selected_indices]
    report = {
        "selector": selector_name,
        "input_count": len(frames),
        "selected_count": len(selected),
        "selected": [{"ts": frame.ts, "path": str(frame.path.resolve())} for frame in selected],
        "sources": {source: selector.report() for source, (selector, _) in selectors.items()},
    }
    progress(1.0, f"本地筛选 {len(frames)} 张 → {len(selected)} 张代表图")
    return selected, report

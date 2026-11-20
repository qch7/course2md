"""路径与时间格式化的小工具。"""

from __future__ import annotations

import re
from pathlib import Path

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_name(text: str, fallback: str = "course") -> str:
    """把课程名压成安全的文件名，保留中文。"""
    name = _ILLEGAL.sub("_", text).strip(" ._")
    return (name or fallback)[:120]


def hhmmss(seconds: float, sep: str = ":") -> str:
    """秒数转 时:分:秒（不含毫秒）。"""
    ms = round(max(seconds, 0.0) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, _ = divmod(rem, 1000)
    return f"{h:02d}{sep}{m:02d}{sep}{s:02d}"


def srt_time(seconds: float, sep: str = ",") -> str:
    """秒数转 SRT/VTT 时间戳，sep 决定用逗号（SRT）还是点（VTT）。"""
    ms = round(max(seconds, 0.0) * 1000)
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path

"""ffmpeg 定位与视频探测。

Windows 上常见的 ffmpeg 都没进 PATH，而且部分定制版缺 wav 复用器，
所以这里只把 ffmpeg 当作可选的“加速路径”，真正的音频解码由 PyAV 兜底。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 常见安装位置；按顺序探测
KNOWN_PATHS = (Path(r"C:\ffmpeg\bin\ffmpeg.exe"),)
ENV_VAR = "COURSE2MD_FFMPEG"


@dataclass
class MediaInfo:
    duration: float
    has_audio: bool
    width: int = 0
    height: int = 0


def find_ffmpeg() -> str | None:
    """返回可用的 ffmpeg 可执行文件路径，找不到时返回 None。"""
    import os

    env = os.environ.get(ENV_VAR)
    if env and Path(env).is_file():
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in KNOWN_PATHS:
        if path.is_file():
            return str(path)
    return None


def probe(path: Path) -> MediaInfo:
    """读取时长与音轨信息；ffmpeg 不可用时退回 PyAV。"""
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        # ffmpeg -i 无输出文件，信息全在 stderr
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        text = proc.stderr or ""
        duration = 0.0
        for token in text.split():
            if token.startswith("Duration:"):
                stamp = token.split(":", 1)[1].rstrip(",")
                h, m, s = stamp.split(":")
                duration = int(h) * 3600 + int(m) * 60 + float(s)
                break
        has_audio = "Audio:" in text
        width = height = 0
        for line in text.splitlines():
            if "Video:" in line and "x" in line:
                for token in line.replace(",", " ").split():
                    if token.count("x") == 1 and token.split("x")[0].isdigit():
                        w, h = token.split("x")
                        width, height = int(w), int(h)
                        break
                break
        return MediaInfo(duration=duration, has_audio=has_audio, width=width, height=height)

    import av

    with av.open(str(path)) as container:
        duration = float(container.duration or 0) / 1_000_000
        has_audio = bool(container.streams.audio)
        width = height = 0
        if container.streams.video:
            stream = container.streams.video[0]
            width, height = stream.codec_context.width, stream.codec_context.height
    return MediaInfo(duration=duration, has_audio=has_audio, width=width, height=height)


def to_json(info: MediaInfo) -> str:
    return json.dumps(info.__dict__, ensure_ascii=False)

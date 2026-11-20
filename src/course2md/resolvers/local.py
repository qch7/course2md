"""本机文件/目录来源。"""

from __future__ import annotations

from pathlib import Path

from .base import BaseResolver, MediaRef, ResolverError, SessionInfo

VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".m4v", ".webm", ".avi", ".mp3", ".m4a", ".wav"}


class LocalResolver(BaseResolver):
    """处理本地视频文件或包含视频的目录。最后匹配，插件可以优先认领特殊路径。"""

    name = "local"
    label = "本地视频"
    source_hint = "本地视频文件或目录"
    priority = -100

    def matches(self, source: str) -> bool:
        if source.startswith(("http://", "https://")):
            return False
        return Path(source).exists()

    def list_sessions(self, source: str) -> list[SessionInfo]:
        path = Path(source)
        if path.is_file():
            course = path.parent.name or "本地视频"
            return [
                SessionInfo(
                    session_id=path.name, date="", title=path.stem, views=1, course=course, meta={"course": course}
                )
            ]
        if path.is_dir():
            files = [p for p in sorted(path.iterdir()) if p.suffix.lower() in VIDEO_SUFFIXES]
            if not files:
                raise ResolverError(f"{path} 下没有可处理的音视频文件")
            course = path.name or "本地视频"
            return [
                SessionInfo(session_id=p.name, date="", title=p.stem, views=1, course=course, meta={"course": course})
                for p in files
            ]
        raise ResolverError(f"找不到 {source}")

    def resolve(self, source: str, session_id: str | None = None, views: list[int] | None = None) -> list[MediaRef]:
        path = Path(source)
        if path.is_dir():
            if session_id:
                path = path / session_id
            else:
                candidates = [p for p in sorted(path.iterdir()) if p.suffix.lower() in VIDEO_SUFFIXES]
                if not candidates:
                    raise ResolverError(f"{path} 下没有可处理的音视频文件")
                path = candidates[0]
        if not path.is_file():
            raise ResolverError(f"找不到 {path}")
        return [MediaRef(url=str(path), name=path.name, view=1)]

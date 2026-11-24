"""阶段之间传递的产物。

每种产物都能与 JSON 互转（``to_dict`` / ``from_dict``），格式带版本号
``ARTIFACT_VERSION``。这样一个阶段的输出可以落盘、上传或交给其它节点，再由
后续阶段读回，而不依赖同一进程里的对象。路径统一保存为字符串。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..asr.formats import Segment
from ..asr.frames import Frame
from ..resolvers.base import SessionInfo

ARTIFACT_VERSION = 1


def _check(data: dict[str, Any], kind: str) -> None:
    if data.get("kind") != kind:
        raise ValueError(f"产物类型不符：需要 {kind}，得到 {data.get('kind')!r}")
    version = data.get("version")
    if version != ARTIFACT_VERSION:
        raise ValueError(f"不支持的产物版本 {version!r}（当前 {ARTIFACT_VERSION}）")


@dataclass
class MediaFile:
    """媒体阶段准备好的一路本地视频。remote 表示由本任务下载，可在结束后清理。"""

    path: Path
    name: str
    remote: bool = False
    view: int | None = None
    primary: bool = False
    meta: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "media-file",
            "version": ARTIFACT_VERSION,
            "path": str(self.path),
            "name": self.name,
            "remote": self.remote,
            "view": self.view,
            "primary": self.primary,
            "meta": dict(self.meta),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MediaFile:
        _check(data, "media-file")
        return cls(
            path=Path(data["path"]),
            name=data["name"],
            remote=bool(data.get("remote")),
            view=data.get("view"),
            primary=bool(data.get("primary")),
            meta={str(k): str(v) for k, v in (data.get("meta") or {}).items()},
        )


@dataclass
class Transcript:
    """一路媒体的转写。meta 是写进转写文件头部的展示信息，corrected 为校对改动数。"""

    stem: str
    media: MediaFile
    duration: float
    segments: list[Segment]
    meta: dict[str, str] = field(default_factory=dict)
    corrected: int = 0

    @property
    def primary(self) -> bool:
        return self.media.primary

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "transcript",
            "version": ARTIFACT_VERSION,
            "stem": self.stem,
            "media": self.media.to_dict(),
            "duration": self.duration,
            "segments": [{"start": s.start, "end": s.end, "text": s.text} for s in self.segments],
            "meta": dict(self.meta),
            "corrected": self.corrected,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        _check(data, "transcript")
        return cls(
            stem=data["stem"],
            media=MediaFile.from_dict(data["media"]),
            duration=float(data["duration"]),
            segments=[Segment(float(s["start"]), float(s["end"]), str(s["text"])) for s in data.get("segments") or []],
            meta={str(k): str(v) for k, v in (data.get("meta") or {}).items()},
            corrected=int(data.get("corrected") or 0),
        )


@dataclass
class FrameSet:
    """画面阶段为一路媒体选出的截图与索引文件。"""

    stem: str
    frames: list[Frame] = field(default_factory=list)
    index: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "frame-set",
            "version": ARTIFACT_VERSION,
            "stem": self.stem,
            "frames": [{"ts": f.ts, "path": str(f.path)} for f in self.frames],
            "index": str(self.index) if self.index else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameSet:
        _check(data, "frame-set")
        return cls(
            stem=data["stem"],
            frames=[Frame(float(f["ts"]), Path(f["path"])) for f in data.get("frames") or []],
            index=Path(data["index"]) if data.get("index") else None,
        )


@dataclass
class TranscriptFiles:
    """输出阶段为一路媒体写出的转写文件（按格式）与元信息文件。"""

    stem: str
    files: dict[str, Path] = field(default_factory=dict)
    meta_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "transcript-files",
            "version": ARTIFACT_VERSION,
            "stem": self.stem,
            "files": {fmt: str(path) for fmt, path in self.files.items()},
            "meta_path": str(self.meta_path) if self.meta_path else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptFiles:
        _check(data, "transcript-files")
        return cls(
            stem=data["stem"],
            files={fmt: Path(path) for fmt, path in (data.get("files") or {}).items()},
            meta_path=Path(data["meta_path"]) if data.get("meta_path") else None,
        )


@dataclass
class Notes:
    """整理阶段写出的两版笔记与 Agent 工作区。"""

    detailed: Path
    brief: Path
    workspace: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "notes",
            "version": ARTIFACT_VERSION,
            "detailed": str(self.detailed),
            "brief": str(self.brief),
            "workspace": str(self.workspace) if self.workspace else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Notes:
        _check(data, "notes")
        return cls(
            detailed=Path(data["detailed"]),
            brief=Path(data["brief"]),
            workspace=Path(data["workspace"]) if data.get("workspace") else None,
        )


@dataclass
class MediaOutput:
    """媒体阶段的完整输出：全部本地视频（用于清理）与有效转写。"""

    files: list[MediaFile] = field(default_factory=list)
    transcripts: list[Transcript] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "media-output",
            "version": ARTIFACT_VERSION,
            "files": [item.to_dict() for item in self.files],
            "transcripts": [item.to_dict() for item in self.transcripts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MediaOutput:
        _check(data, "media-output")
        return cls(
            files=[MediaFile.from_dict(item) for item in data.get("files") or []],
            transcripts=[Transcript.from_dict(item) for item in data.get("transcripts") or []],
        )


@dataclass
class OrganizeInput:
    """整理阶段的输入：主音轨转写与全部画面。"""

    transcript: Transcript
    frames: list[FrameSet] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "organize-input",
            "version": ARTIFACT_VERSION,
            "transcript": self.transcript.to_dict(),
            "frames": [item.to_dict() for item in self.frames],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrganizeInput:
        _check(data, "organize-input")
        return cls(
            transcript=Transcript.from_dict(data["transcript"]),
            frames=[FrameSet.from_dict(item) for item in data.get("frames") or []],
        )


@dataclass
class ChatInput:
    workspace: Path
    detailed: Path
    message: str
    history: list[dict]
    files: list[Path] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "chat-input",
            "version": ARTIFACT_VERSION,
            "workspace": str(self.workspace),
            "detailed": str(self.detailed),
            "message": self.message,
            "history": self.history,
            "files": [str(path) for path in self.files],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatInput:
        _check(data, "chat-input")
        return cls(
            Path(data["workspace"]),
            Path(data["detailed"]),
            data["message"],
            data["history"],
            [Path(path) for path in data.get("files", [])],
        )


@dataclass
class ChatOutput:
    text: str
    updated: bool

    def to_dict(self) -> dict[str, Any]:
        return {"kind": "chat-output", "version": ARTIFACT_VERSION, "text": self.text, "updated": self.updated}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatOutput:
        _check(data, "chat-output")
        return cls(data["text"], bool(data["updated"]))


@dataclass
class SessionList:
    """解析阶段在节点上列出的课次（来源解析器的 ``list_sessions`` 结果）。"""

    sessions: list[SessionInfo] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "session-list",
            "version": ARTIFACT_VERSION,
            "sessions": [
                {
                    "session_id": item.session_id,
                    "date": item.date,
                    "title": item.title,
                    "views": item.views,
                    "week": item.week,
                    "course": item.course,
                    "meta": dict(item.meta),
                }
                for item in self.sessions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionList:
        _check(data, "session-list")
        return cls(
            [
                SessionInfo(
                    session_id=str(item["session_id"]),
                    date=str(item.get("date") or ""),
                    title=str(item.get("title") or ""),
                    views=int(item.get("views") or 0),
                    week=item.get("week"),
                    course=str(item.get("course") or ""),
                    meta={str(k): str(v) for k, v in (item.get("meta") or {}).items()},
                )
                for item in data.get("sessions") or []
            ]
        )


Artifact = (
    MediaFile
    | Transcript
    | FrameSet
    | TranscriptFiles
    | Notes
    | MediaOutput
    | OrganizeInput
    | ChatInput
    | ChatOutput
    | SessionList
)

ARTIFACT_TYPES: dict[str, Any] = {
    "session-list": SessionList,
    "chat-input": ChatInput,
    "chat-output": ChatOutput,
    "media-file": MediaFile,
    "transcript": Transcript,
    "frame-set": FrameSet,
    "transcript-files": TranscriptFiles,
    "notes": Notes,
    "media-output": MediaOutput,
    "organize-input": OrganizeInput,
}


def load_artifact(data: dict[str, Any]) -> Artifact:
    """按 kind 字段还原任意产物。"""
    kind = data.get("kind")
    if kind not in ARTIFACT_TYPES:
        raise ValueError(f"未知产物类型：{kind!r}")
    return ARTIFACT_TYPES[kind].from_dict(data)

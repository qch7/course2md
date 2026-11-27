"""媒体阶段：解析来源、下载、抽音频、转写。

输入为任务选项中的来源，输出每一路本地视频（:class:`MediaFile`）与转写
（:class:`Transcript`）。音频过短的媒体只保留视频记录，不产生转写。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ..asr import audio as audio_mod
from ..asr.whisper import WhisperBackend
from ..resolvers import ResolverError, download, get_resolver, public_source
from ..resolvers.base import MediaRef
from ..util.text import ensure_dir, hhmmss, safe_name
from .artifacts import MediaFile, MediaOutput, Transcript
from .base import BaseStage, StageContext

if TYPE_CHECKING:
    from .options import JobOptions


class MediaStage(BaseStage[None, MediaOutput]):
    """backend 可由调用方传入复用；配置不一致时换成新模型，运行后保存在 self.backend。"""

    name: ClassVar[str] = "media"
    label: ClassVar[str] = "转写"
    cache_options: ClassVar[tuple[str, ...] | None] = ("source", "session_id", "views", "out_dir", "asr")
    anchors_node: ClassVar[bool] = True

    def __init__(self, backend: WhisperBackend | None = None) -> None:
        self.backend = backend

    @classmethod
    def create(cls, **resources: Any) -> MediaStage:
        return cls(resources.get("backend"))

    def resources(self) -> dict[str, Any]:
        return {"backend": self.backend} if self.backend is not None else {}

    @classmethod
    def requires(cls, options: JobOptions, inputs: None = None) -> list[str]:
        try:
            name = get_resolver(options.source).name
        except Exception:  # noqa: BLE001  解析器缺失时仍允许排队，执行时报告原因
            return ["asr"]
        # 本地文件只存在于 Hub 所在电脑，由内置节点处理
        return ["asr", "resolver:local", "hub"] if name == "local" else ["asr", f"resolver:{name}"]

    @classmethod
    def model(cls, options: JobOptions) -> str:
        return f"faster-whisper {options.asr.model}"

    def run(self, ctx: StageContext, inputs: None = None) -> MediaOutput:
        options = ctx.options
        media_dir = ensure_dir(options.out_dir / "media")

        ctx.progress("解析", 0.02, f"解析来源：{public_source(options.source)[:80]}", None)
        resolver = get_resolver(options.source)
        refs = resolver.resolve(options.source, options.session_id, options.views)
        if not refs:
            raise ResolverError("没有解析到任何媒体")
        ctx.progress("解析", 0.06, f"解析到 {len(refs)} 路机位：{', '.join(r.name for r in refs)}", None)

        output = MediaOutput()
        for index, ref in enumerate(refs, 1):
            ctx.check()
            output.files.append(_fetch_media_file(ctx, media_dir, ref, index, len(refs)))

        if self.backend is None or self.backend.cfg != options.asr:
            self.backend = WhisperBackend(options.asr)
        backend = self.backend
        total = len(output.files)
        for index, media in enumerate(output.files, 1):
            ctx.check()
            transcript = _transcribe_media(ctx, backend, media, index, total)
            if transcript is not None:
                output.transcripts.append(transcript)
        _remove_empty_audio_dir(options.out_dir / "audio", options.keep_audio)
        return output


def _fetch_media_file(ctx: StageContext, media_dir: Path, ref: MediaRef, index: int, count: int) -> MediaFile:
    """远程机位下载到 media 目录（已存在则跳过），本地机位直接引用原路径。"""
    progress = ctx.progress
    if ref.is_remote:
        dest = media_dir / safe_name(ref.name)
        share = (index - 1) / count
        if dest.exists() and dest.stat().st_size > 0:
            progress("下载", 0.08 + 0.22 * index / count, f"已存在，跳过：{dest.name}", 1.0)
        else:
            progress("下载", 0.08 + 0.22 * share, f"开始下载 {ref.name}", 0.0)

            def on_bytes(have: int, total: int, ref: MediaRef = ref, share: float = share) -> None:
                ratio = min(1.0, have / total) if total else 0.0
                progress(
                    "下载",
                    0.08 + 0.22 * (share + ratio / count),
                    f"{ref.name}  {have / 1048576:.0f} / {total / 1048576:.0f} MB",
                    ratio,
                )

            import requests

            with requests.Session() as http:
                download(ref, dest, progress=on_bytes, session=http)
    else:
        dest = Path(ref.url)
    return MediaFile(
        path=dest,
        name=ref.name,
        remote=ref.is_remote,
        view=ref.view,
        primary=bool(getattr(ref, "primary", False)),
        meta=dict(ref.meta),
    )


def _transcribe_media(
    ctx: StageContext, backend: WhisperBackend, media: MediaFile, index: int, total: int
) -> Transcript | None:
    """抽音频并转写一路媒体；音频过短时返回 None。"""
    options = ctx.options
    progress = ctx.progress
    base = (index - 1) / total
    span = 1.0 / total
    progress("音频", 0.30 + base * 0.04, f"抽取音频：{media.path.name}", None)
    wav_path = options.out_dir / "audio" / f"{safe_name(media.path.stem)}.wav"
    wav: Path | None = None
    try:
        wav, duration = audio_mod.prepare(media.path, wav_path)
        if duration < 5:
            ctx.warn(f"{media.path.name} 音频过短（{duration:.1f}s），跳过")
            return None

        def on_backend(fraction: float, message: str, base: float = base, span: float = span) -> None:
            progress("转写", 0.34 + base * 0.22 + fraction * span * 0.22, message, fraction)

        backend.progress = on_backend
        backend.check_cancel = ctx.check
        try:
            segments = backend.transcribe(audio_mod.read_wav(wav))
        finally:
            backend.check_cancel = None
    finally:
        if wav is not None and not options.keep_audio:
            wav.unlink(missing_ok=True)
    stem = safe_name(media.path.stem)
    return Transcript(
        stem=stem,
        media=media,
        duration=duration,
        segments=segments,
        meta={
            "课程": str(media.meta.get("subject") or stem),
            "节次": str(media.meta.get("begin") or media.path.stem),
            "时长": hhmmss(duration),
            "机位": str(media.view or 1),
            "转写": f"faster-whisper {options.asr.model}（{options.asr.device}）",
        },
    )


def _remove_empty_audio_dir(audio_dir: Path, keep_audio: bool) -> None:
    """清掉转写后留下的空 audio 目录；目录非空（保留了 WAV）时 rmdir 会失败，忽略即可。"""
    if not keep_audio and audio_dir.is_dir():
        try:
            audio_dir.rmdir()
        except OSError:
            pass


def select_primary(transcripts: list[Transcript]) -> Transcript:
    """笔记主音轨：解析器标记的主音轨，其次机位 1，最后第一路有讲话的媒体。"""
    if not transcripts:
        raise RuntimeError("没有得到任何转写内容")
    audible = [item for item in transcripts if item.segments]
    if not audible:
        raise RuntimeError("没有识别到讲话内容，请检查音轨或转写参数")
    return next((item for item in audible if item.primary), None) or next(
        (item for item in audible if item.meta["机位"] == "1"), audible[0]
    )

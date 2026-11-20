"""输出阶段：把定稿转写按所选格式写成文件，并写出每路的元信息。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ..asr.formats import Segment, merge_paragraphs, to_json, to_markdown, to_srt, to_txt, to_vtt
from .artifacts import Transcript, TranscriptFiles
from .base import BaseStage, StageContext

if TYPE_CHECKING:
    from .options import JobOptions

TRANSCRIPT_FORMATS = ("srt", "vtt", "txt", "md", "json")


def check_formats(formats: tuple[str, ...] | list[str]) -> None:
    unknown = [fmt for fmt in formats if fmt not in TRANSCRIPT_FORMATS]
    if unknown:
        raise ValueError(f"未知输出格式：{', '.join(unknown)}，可选 {', '.join(TRANSCRIPT_FORMATS)}")


class OutputStage(BaseStage[list[Transcript], list[TranscriptFiles]]):
    name: ClassVar[str] = "output"
    label: ClassVar[str] = "输出"
    cache_options: ClassVar[tuple[str, ...] | None] = ("out_dir", "formats")

    @classmethod
    def requires(cls, options: JobOptions, inputs: list[Transcript] | None = None) -> list[str]:
        return ["hub"]  # 转写文件写进 Hub 的输出目录

    def run(self, ctx: StageContext, inputs: list[Transcript]) -> list[TranscriptFiles]:
        options = ctx.options
        results: list[TranscriptFiles] = []
        for index, item in enumerate(inputs, 1):
            meta = dict(item.meta)
            meta["校对"] = f"AI 校对（{item.corrected} 处）" if item.corrected else "未改动"
            paragraphs = merge_paragraphs(item.segments)
            ctx.progress("输出", 0.66 + index * 0.01, f"写出 {item.stem} 的转写文件", None)
            files = {
                fmt: write_format(options.out_dir, item.stem, fmt, item.segments, paragraphs, item.stem, meta)
                for fmt in options.formats
            }
            meta_path = options.out_dir / f"{item.stem}.meta.json"
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            results.append(TranscriptFiles(stem=item.stem, files=files, meta_path=meta_path))
        return results


def write_format(
    out_dir: Path, stem: str, fmt: str, segments: list[Segment], paragraphs: list, title: str, meta: dict[str, str]
) -> Path:
    """按格式写出单个转写文件。"""
    path = out_dir / f"{stem}.{fmt}"
    if fmt == "srt":
        path.write_text(to_srt(segments), encoding="utf-8")
    elif fmt == "vtt":
        path.write_text(to_vtt(segments), encoding="utf-8")
    elif fmt == "txt":
        path.write_text(to_txt(segments, paragraphs), encoding="utf-8")
    elif fmt == "md":
        path.write_text(to_markdown(segments, title, meta, paragraphs), encoding="utf-8")
    elif fmt == "json":
        path.write_text(to_json(segments, title, meta), encoding="utf-8")
    else:
        raise ValueError(f"未知格式：{fmt}")
    return path

"""转写结果的数据结构与多种输出格式。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from ..util.text import hhmmss, srt_time

TS_LINE = re.compile(r"^\*\*\[(\d\d):(\d\d):(\d\d)\]\*\*\s*(.*)$")
SRT_TS = re.compile(r"^(\d\d:\d\d:\d\d)[,.](\d\d\d)\s*-->\s*(\d\d:\d\d:\d\d)[,.](\d\d\d)$")


@dataclass
class Segment:
    """一条语音片段，时间为相对音频起点的秒数。"""

    start: float
    end: float
    text: str

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Paragraph:
    """由若干片段合并而成的可读段落。"""

    start: float
    end: float
    text: str


def merge_paragraphs(segments: list[Segment], gap: float = 1.6, max_len: int = 150) -> list[Paragraph]:
    """把碎片合并成段落：遇到句末标点、超长或长停顿就断开。"""
    out: list[Paragraph] = []
    buf: list[str] = []
    start = end = 0.0
    last_end: float | None = None
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        if buf and last_end is not None and seg.start - last_end > gap:
            out.append(Paragraph(start, end, "，".join(buf)))
            buf = []
        if not buf:
            start = seg.start
        buf.append(text)
        end = last_end = seg.end
        joined = "，".join(buf)
        if joined[-1] in "。！？!?；;…" or len(joined) >= max_len:
            out.append(Paragraph(start, end, joined))
            buf = []
    if buf:
        out.append(Paragraph(start, end, "，".join(buf)))
    return out


def to_srt(segments: list[Segment]) -> str:
    blocks = [f"{i}\n{srt_time(s.start)} --> {srt_time(s.end)}\n{s.text}\n" for i, s in enumerate(segments, 1)]
    return "\n".join(blocks)


def to_vtt(segments: list[Segment]) -> str:
    blocks = ["WEBVTT\n"]
    for seg in segments:
        blocks.append(f"{srt_time(seg.start, '.')} --> {srt_time(seg.end, '.')}\n{seg.text}\n")
    return "\n".join(blocks)


def to_txt(segments: list[Segment], paragraphs: list[Paragraph] | None = None) -> str:
    paras = paragraphs if paragraphs is not None else merge_paragraphs(segments)
    return "\n\n".join(p.text for p in paras) + "\n"


def to_markdown(
    segments: list[Segment], title: str, meta: dict[str, str] | None = None, paragraphs: list[Paragraph] | None = None
) -> str:
    """带时间轴锚点的 Obsidian 友好版本，AI 整理阶段也读这个格式。"""
    paras = paragraphs if paragraphs is not None else merge_paragraphs(segments)
    lines = [f"# {title}", ""]
    for key, value in (meta or {}).items():
        lines.append(f"- {key}：{value}")
    if meta:
        lines.append("")
    lines += ["---", ""]
    for para in paras:
        lines += [f"**[{hhmmss(para.start)}]** {para.text}", ""]
    return "\n".join(lines)


def to_json(segments: list[Segment], title: str, meta: dict[str, str] | None = None) -> str:
    return json.dumps(
        {
            "title": title,
            "meta": meta or {},
            "segments": [asdict(s) for s in segments],
        },
        ensure_ascii=False,
        indent=2,
    )


def parse_markdown(text: str) -> list[Segment]:
    """从本工具产出的 Markdown 里还原带时间戳的片段。"""
    segments: list[Segment] = []
    for line in text.splitlines():
        match = TS_LINE.match(line.strip())
        if not match:
            continue
        h, m, s = (int(match.group(i)) for i in (1, 2, 3))
        body = match.group(4).strip()
        if body:
            start = h * 3600 + m * 60 + s
            segments.append(Segment(start, start, body))
    return segments


def parse_srt(text: str) -> list[Segment]:
    segments: list[Segment] = []
    for block in text.strip().split("\n\n"):
        lines = block.split("\n")
        if len(lines) < 3:
            continue
        match = SRT_TS.match(lines[1].strip())
        if not match:
            continue
        h1, m1, s1 = lines[1].split(" --> ")[0].replace(",", ".").split(":")
        h2, m2, s2 = lines[1].split(" --> ")[1].replace(",", ".").split(":")
        segments.append(
            Segment(
                int(h1) * 3600 + int(m1) * 60 + float(s1),
                int(h2) * 3600 + int(m2) * 60 + float(s2),
                " ".join(lines[2:]).strip(),
            )
        )
    return segments


def load_segments(path: Path) -> list[Segment]:
    """按扩展名读取已有的转写结果（srt / md）。"""
    text = Path(path).read_text(encoding="utf-8")
    if path.suffix.lower() == ".srt":
        return parse_srt(text)
    return parse_markdown(text)

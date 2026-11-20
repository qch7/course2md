"""把转写稿整理成结构化笔记：分块提炼 → 逐层合并。

长课程（2 小时以上）的转写稿超出一次性提示的舒适区，
所以先按时间切块提炼要点，再合并；要点过多时再折一轮。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..util.text import hhmmss
from . import prompts
from .llm import ChatClient

Progress = Callable[[float, str], None]


@dataclass
class Chunk:
    """一段带时间范围的转写切片。"""

    index: int
    start: float
    end: float
    body: str


def _noop(_fraction: float, _message: str) -> None:
    return None


def build_chunks(paragraphs: list, chunk_chars: int) -> list[Chunk]:
    """按段落边界贪心切块，不切断句子。"""
    chunks: list[Chunk] = []
    buffer: list[str] = []
    size = 0
    start = end = 0.0
    for para in paragraphs:
        piece = f"[{hhmmss(para.start)}] {para.text}"
        if not buffer:
            start = para.start
        buffer.append(piece)
        size += len(piece)
        end = para.end
        if size >= chunk_chars:
            chunks.append(Chunk(len(chunks) + 1, start, end, "\n".join(buffer)))
            buffer, size = [], 0
    if buffer:
        chunks.append(Chunk(len(chunks) + 1, start, end, "\n".join(buffer)))
    return chunks


def _chat(client: ChatClient, system: str, user: str, max_tokens: int | None = None) -> str:
    return client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=max_tokens,
    )


def summarize_chunks(chunks: list[Chunk], title: str, client: ChatClient, progress: Progress = _noop) -> list[str]:
    """逐块提炼要点。"""
    notes: list[str] = []
    for chunk in chunks:
        progress(
            0.1 + 0.6 * chunk.index / max(len(chunks), 1),
            f"提炼第 {chunk.index}/{len(chunks)} 段（{hhmmss(chunk.start)}-{hhmmss(chunk.end)}）",
        )
        user = prompts.CHUNK_TEMPLATE.format(
            title=title,
            index=chunk.index,
            total=len(chunks),
            start=hhmmss(chunk.start),
            end=hhmmss(chunk.end),
            body=chunk.body,
        )
        notes.append(_chat(client, prompts.CHUNK_SYSTEM, user).strip())
    return notes

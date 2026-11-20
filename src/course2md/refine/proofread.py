"""用大模型校对转写稿：只让模型回「需要改的行」，不要求回显全文。

为什么要差异清单：让模型把 1300 行原样回显，输出接近 10k token，
既慢又容易被 max_tokens 截断；只回改动过的几十行，输出小一个数量级。

回填时按「时间戳 + 原文」双重匹配：原文对不上的条目直接跳过，
所以模型编造或错位都不会污染转写结果。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from ..asr.formats import Segment
from ..config import LlmConfig
from ..util.text import hhmmss
from . import prompts
from .llm import ChatClient, LlmError

Progress = Callable[[float, str], None]


def _noop(_fraction: float, _message: str) -> None:
    return None


@dataclass
class ProofreadResult:
    """校对结果：新片段、改动条数、被跳过的条目数。"""

    segments: list[Segment]
    changed: int
    rejected: int = 0


def _chunk(segments: list[Segment], limit: int) -> list[list[Segment]]:
    """按字符数切块，保持片段完整。"""
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for seg in segments:
        line_len = len(seg.text) + 12
        if current and size + line_len > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(seg)
        size += line_len
    if current:
        chunks.append(current)
    return chunks


def _render(chunk: list[Segment]) -> str:
    return "\n".join(f"[{hhmmss(s.start)}] {s.text}" for s in chunk)


def parse_corrections(text: str) -> list[dict]:
    """从模型输出里取出 JSON 数组；容忍被代码块包裹或夹带说明文字。"""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = fenced.group(1) if fenced else text
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return []
    return [item for item in data if isinstance(item, dict)]


def apply_corrections(segments: list[Segment], entries: list[dict]) -> tuple[list[Segment], int, int]:
    """按时间戳和必填原文精确替换。拒绝歧义与重复改动，返回片段及计数。"""
    index: dict[tuple[str, str], int | None] = {}
    for i, seg in enumerate(segments):
        key = (hhmmss(seg.start), seg.text.strip())
        index[key] = None if key in index else i
    fixed = list(segments)
    changed = rejected = 0
    for entry in entries:
        ts, old, new = entry.get("ts"), entry.get("old"), entry.get("new")
        if (
            not isinstance(ts, str)
            or not isinstance(old, str)
            or not old.strip()
            or not isinstance(new, str)
            or not new.strip()
        ):
            rejected += 1
            continue
        pos = index.pop((ts, old.strip()), None)
        if pos is None:
            rejected += 1
            continue
        target = fixed[pos]
        fixed[pos] = Segment(target.start, target.end, new.strip())
        changed += 1
    return fixed, changed, rejected


def proofread(
    segments: list[Segment], title: str, cfg: LlmConfig, client: ChatClient | None = None, progress: Progress = _noop
) -> ProofreadResult:
    """校对整份转写稿，只接受能对上原文的改动。"""
    client = client or ChatClient(cfg)
    chunks = _chunk(segments, limit=max(2000, min(cfg.chunk_chars, 8000)))
    result: list[Segment] = []
    changed = rejected = 0

    for index, chunk in enumerate(chunks, 1):
        progress(
            0.1 + 0.85 * index / max(len(chunks), 1), f"校对第 {index}/{len(chunks)} 段（{hhmmss(chunk[0].start)} 起）"
        )
        user = prompts.PROOFREAD_TEMPLATE.format(title=title, index=index, total=len(chunks), body=_render(chunk))
        try:
            reply = client.chat(
                [{"role": "system", "content": prompts.PROOFREAD_SYSTEM}, {"role": "user", "content": user}],
                temperature=0.0,
            )
        except LlmError as exc:
            if "鉴权" in str(exc) or "未配置" in str(exc):
                raise
            result.extend(chunk)
            continue

        fixed, chunk_changed, chunk_rejected = apply_corrections(chunk, parse_corrections(reply))
        changed += chunk_changed
        rejected += chunk_rejected
        result.extend(fixed)

    progress(1.0, f"校对完成：改动 {changed} 处，跳过 {rejected} 条")
    return ProofreadResult(result, changed, rejected)

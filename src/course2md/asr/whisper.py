"""faster-whisper 后端：分片转写、幻觉过滤、空隙回填。

两个来自实测的硬约束：
1. `condition_on_previous_text=True` 时，开头一句幻觉会被一路带下去污染全片；
2. VAD 会把远场的低声讲话整段切掉，而且这些片段往往挤在整段音频里，
   单看某一段听不出来，只能在时间轴上按空隙回填。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

from ..config import AsrConfig, bootstrap_runtime, find_model
from .audio import SAMPLE_RATE
from .filters import is_hallucination
from .formats import Segment

Progress = Callable[[float, str], None]


def _noop(_fraction: float, _message: str) -> None:
    return None


class WhisperBackend:
    """封装模型加载与转写，模型只加载一次可重复使用。"""

    def __init__(self, cfg: AsrConfig, progress: Progress | None = None) -> None:
        self.cfg = cfg
        self.progress = progress or _noop
        # 转写按片段惰性产出，每段之后调用一次，抛出异常即可中止本次转写。
        self.check_cancel: Callable[[], None] | None = None
        self._model = None

    # ---------------------------------------------------------------- 模型
    def load(self):
        """加载模型；依赖目录与本地模型都会自动定位。"""
        if self._model is not None:
            return self._model
        bootstrap_runtime()
        from faster_whisper import WhisperModel

        model_path = find_model(self.cfg.model)
        self.progress(0.02, f"加载模型 {model_path}（{self.cfg.device}/{self.cfg.compute_type}）")
        kwargs: dict[str, Any] = {"device": self.cfg.device, "compute_type": self.cfg.compute_type}
        if self.cfg.cpu_threads:
            kwargs["cpu_threads"] = self.cfg.cpu_threads
        self._model = WhisperModel(model_path, **kwargs)
        return self._model

    # ---------------------------------------------------------------- 转写
    def _decode(
        self, window: np.ndarray, vad: bool, progress: Progress | None = None, offset: float = 0.0, span: float = 0.0
    ) -> list[Segment]:
        """转写一段音频，返回过滤后的片段。"""
        model = self.load()
        segments, _info = model.transcribe(
            window,
            language=self.cfg.language,
            beam_size=self.cfg.beam_size,
            vad_filter=vad,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            condition_on_previous_text=False,
            initial_prompt=self.cfg.initial_prompt or None,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            no_speech_threshold=0.6,
            hallucination_silence_threshold=2.0,
            word_timestamps=False,
        )
        out: list[Segment] = []
        for seg in segments:
            if self.check_cancel:
                self.check_cancel()
            text = seg.text.strip()
            if not text or is_hallucination(text):
                continue
            out.append(Segment(offset + seg.start, offset + seg.end, text))
            if progress and span:
                # 上限压在 0.88，给后面的空隙回填留出进度空间
                progress(min(0.88 * (offset + seg.end) / span, 0.88), f"已转写 {int(offset + seg.end)}s")
        return out

    def transcribe(self, samples: np.ndarray) -> list[Segment]:
        """整段转写，并按空隙回填被 VAD 吞掉的内容。"""
        total = samples.size / SAMPLE_RATE
        self.progress(0.05, f"开始转写，共 {total:.0f}s")

        segments = self._decode(samples, self.cfg.vad, self.progress, 0.0, total)
        segments.sort(key=lambda s: s.start)
        self.progress(0.90, f"主转写完成：{len(segments)} 段")

        gaps = self.find_gaps(segments, total)
        if gaps:
            span_text = "、".join(f"{start:.0f}-{end:.0f}s" for start, end in gaps)
            self.progress(0.91, f"发现 {len(gaps)} 处空档，回填：{span_text}")
        repaired = self.repair_gaps(samples, segments)
        if repaired:
            merged = {round(s.start, 2): s for s in segments}
            for seg in repaired:
                merged[round(seg.start, 2)] = seg
            segments = sorted(merged.values(), key=lambda s: s.start)
            self.progress(0.96, f"回填空隙 {len(repaired)} 段，共 {len(segments)} 段")
        return segments

    # ------------------------------------------------------------ 空隙回填
    def find_gaps(self, segments: list[Segment], total: float) -> list[tuple[float, float]]:
        """返回值得回填的时间空隙。"""
        gaps: list[tuple[float, float]] = []
        cursor = max(0.0, self.cfg.leading_skip_seconds)
        for seg in segments:
            if seg.start - cursor > self.cfg.gap_repair_seconds:
                gaps.append((cursor, seg.start))
            cursor = max(cursor, seg.end)
        if total - cursor > self.cfg.gap_repair_seconds * 2:
            gaps.append((cursor, total))
        return gaps

    def repair_gaps(self, samples: np.ndarray, segments: list[Segment]) -> list[Segment]:
        """对每个空隙做不带 VAD 的重转写，补回被误切的讲话。"""
        total = samples.size / SAMPLE_RATE
        gaps = self.find_gaps(segments, total)
        if not gaps:
            return []
        recovered: list[Segment] = []
        for start, end in gaps:
            if self.check_cancel:
                self.check_cancel()
            window = samples[int(start * SAMPLE_RATE) : int(end * SAMPLE_RATE)]
            if window.size < SAMPLE_RATE * 0.5:
                continue
            for seg in self._decode(window, vad=False, offset=start):
                # 空隙边缘常见的半句重复，掐掉跨界的尾巴
                if seg.end <= end + 0.5:
                    recovered.append(seg)
        return recovered

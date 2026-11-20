"""校对阶段：逐路调用模型校对转写。失败只记警告，保留原转写。"""

from __future__ import annotations

from dataclasses import replace
from importlib import import_module
from typing import TYPE_CHECKING, ClassVar

from .artifacts import Transcript
from .base import BaseStage, StageContext

if TYPE_CHECKING:
    from .options import JobOptions


class ProofreadStage(BaseStage[list[Transcript], list[Transcript]]):
    name: ClassVar[str] = "proofread"
    label: ClassVar[str] = "校对"
    cache_options: ClassVar[tuple[str, ...] | None] = ("out_dir", "llm")

    @classmethod
    def requires(cls, options: JobOptions, inputs: list[Transcript] | None = None) -> list[str]:
        return ["llm-api"]

    @classmethod
    def model(cls, options: JobOptions) -> str:
        return options.llm.model

    def run(self, ctx: StageContext, inputs: list[Transcript]) -> list[Transcript]:
        # refine 包把同名函数导出为 refine.proofread，这里需要的是模块本身。
        proofread_mod = import_module("course2md.refine.proofread")

        results = list(inputs)
        for index, item in enumerate(inputs, 1):
            ctx.check()
            if not item.segments:
                continue
            try:
                proof = proofread_mod.proofread(
                    item.segments,
                    item.stem,
                    ctx.options.llm,
                    progress=lambda f, m, i=index: ctx.progress("校对", 0.58 + (i - 1) * 0.04 + f * 0.04, m, f),
                )
            except Exception as exc:  # noqa: BLE001  校对失败不该毁掉转写结果
                ctx.warn(f"AI 校对未生效：{exc}")
                break
            results[index - 1] = replace(item, segments=proof.segments, corrected=item.corrected + proof.changed)
            if proof.rejected:
                ctx.warn(f"校对中有 {proof.rejected} 条对不上原文，已跳过")
        return results

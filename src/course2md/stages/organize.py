"""整理阶段：用主音轨转写与全部截图生成课程记录，写出详细版与缩略版笔记。"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, ClassVar

from ..util.job_refs import to_job_refs
from ..util.text import ensure_dir
from .artifacts import Notes, OrganizeInput
from .base import BaseStage, StageContext, agent_model, agent_requires

if TYPE_CHECKING:
    from .options import JobOptions


class OrganizeStage(BaseStage[OrganizeInput, Notes]):
    """整理后端（内置模型或 Agent 预设）由任务选项决定，所需能力随之不同。"""

    name: ClassVar[str] = "organize"
    label: ClassVar[str] = "整理"
    cache_options: ClassVar[tuple[str, ...] | None] = ("out_dir", "llm", "agent", "frame_selector")

    @classmethod
    def requires(cls, options: JobOptions, inputs: OrganizeInput | None = None) -> list[str]:
        requires = agent_requires(options)
        # 多张截图时整理阶段要再次筛选，节点须能运行同一策略
        if isinstance(inputs, OrganizeInput) and sum(len(item.frames) for item in inputs.frames) > 1:
            requires.append(f"frame-selector:{options.frame_selector}")
        return requires

    @classmethod
    def model(cls, options: JobOptions) -> str:
        return agent_model(options)

    def run(self, ctx: StageContext, inputs: OrganizeInput) -> Notes:
        from ..refine import lesson

        options = ctx.options
        primary = inputs.transcript
        notes_dir = ensure_dir(options.out_dir / "notes")
        workspace = notes_dir / "agent-workspace" / uuid.uuid4().hex
        pair = lesson.build_note_pair(
            primary.segments,
            primary.stem,
            primary.meta,
            options.llm,
            options.agent,
            workspace,
            frames=[frame for item in inputs.frames for frame in item.frames],
            frame_selector=options.frame_selector,
            progress=lambda f, m: ctx.progress("整理", 0.70 + f * 0.28, m, f),
            cancel=ctx.cancel,
            on_output=ctx.on_output,
        )
        ctx.check()
        context = workspace / "context.json"
        if context.is_file():
            data = json.loads(context.read_text(encoding="utf-8"))
            data["figures"] = to_job_refs(data["figures"], options.out_dir)
            context.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        detailed, brief = lesson.save_note_pair(pair, notes_dir / f"{primary.stem}.notes.md")
        return Notes(detailed=detailed, brief=brief, workspace=workspace)

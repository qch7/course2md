"""追问与笔记修改通过相同的节点队列执行。"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from .artifacts import ChatInput, ChatOutput
from .base import BaseStage, StageContext, agent_model, agent_requires

if TYPE_CHECKING:
    from .options import JobOptions


class ChatStage(BaseStage[ChatInput, ChatOutput]):
    name: ClassVar[str] = "chat"
    label: ClassVar[str] = "对话"

    @classmethod
    def requires(cls, options: JobOptions, inputs: ChatInput | None = None) -> list[str]:
        return agent_requires(options)

    @classmethod
    def model(cls, options: JobOptions) -> str:
        return agent_model(options)

    def run(self, ctx: StageContext, inputs: ChatInput) -> ChatOutput:
        from ..refine.conversation import continue_notes

        ctx.check()
        reply = continue_notes(
            inputs.workspace,
            inputs.message,
            inputs.history,
            inputs.detailed,
            ctx.options.llm,
            ctx.options.agent,
            ctx.on_output,
            ctx.cancel,
        )
        ctx.check()
        return ChatOutput(reply.text, reply.updated)

"""阶段的公共约定：运行上下文、取消、阶段接口。

一个阶段只做一件事：读输入产物、写输出产物，通过上下文汇报进度、检查取消、
记录警告。阶段不关心自己在哪台机器上运行，由执行器（:mod:`.executor`）决定。

阶段**自描述**调度所需的全部信息（展示名、缓存相关选项、所需节点能力、生成模型、
节点亲和），执行器、编解码与节点只通过这些声明工作，不按阶段名分支。
新增阶段 = 新建一个 :class:`BaseStage` 子类并登记到 :mod:`.registry`。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Generic, Protocol, TypeVar

if TYPE_CHECKING:
    from ..agents import AgentOutput
    from .options import JobOptions

Progress = Callable[[str, float, str, "float | None"], None]

In = TypeVar("In")
Out = TypeVar("Out")


def _noop(_stage: str, _fraction: float, _message: str, _sub: float | None = None) -> None:
    return None


class Cancelled(RuntimeError):
    """用户取消了任务。"""


@dataclass
class StageContext:
    """一次任务内各阶段共享的运行环境。"""

    options: JobOptions
    progress: Progress = _noop
    cancel: threading.Event = field(default_factory=threading.Event)
    on_output: AgentOutput | None = None
    warnings: list[str] = field(default_factory=list)

    def check(self) -> None:
        """取消时抛出 :class:`Cancelled`，阶段应在每个耗时步骤之间调用。"""
        if self.cancel.is_set():
            raise Cancelled("任务已取消")

    def warn(self, message: str) -> None:
        self.warnings.append(message)


class Stage(Protocol):
    """执行器眼中的阶段：有名字、能运行。调度信息见 :class:`BaseStage`。"""

    name: ClassVar[str]

    def run(self, ctx: StageContext, inputs: Any) -> Any: ...


class BaseStage(Generic[In, Out]):
    """阶段基类，子类覆盖需要的声明与 :meth:`run`。

    name             稳定标识，写入任务队列与缓存键，改名会使旧缓存失效
    label            面向用户的中文名
    cache_options    输出依赖的任务选项；None 表示全部选项。其余选项变化时可复用旧输出
    anchors_node     执行它的节点成为本任务的“媒体节点”（持有原视频）
    pinned_to_anchor 必须在媒体节点上运行（需要读取原视频）；节点离线时按
                     :meth:`fallback_requires` 交给其它节点
    """

    name: ClassVar[str]
    label: ClassVar[str] = ""
    cache_options: ClassVar[tuple[str, ...] | None] = None
    anchors_node: ClassVar[bool] = False
    pinned_to_anchor: ClassVar[bool] = False

    @classmethod
    def create(cls, **_resources: Any) -> BaseStage[In, Out]:
        """节点按名称创建阶段；resources 为节点上可复用的资源（如已加载的模型）。"""
        return cls()

    @classmethod
    def display_name(cls) -> str:
        return cls.label or cls.name

    @classmethod
    def requires(cls, options: JobOptions, inputs: In | None = None) -> list[str]:
        """执行所需的节点能力，见 :mod:`course2md.worker.capabilities`。"""
        return []

    @classmethod
    def fallback_requires(cls, options: JobOptions) -> list[str]:
        """媒体节点不可用时，其它节点执行本阶段所需的能力（仅 pinned_to_anchor 阶段使用）。"""
        return cls.requires(options)

    @classmethod
    def model(cls, options: JobOptions) -> str:
        """记录在产物上的生成模型，空串表示不涉及模型。"""
        return ""

    def resources(self) -> dict[str, Any]:
        """运行后留给节点复用的资源，下次 :meth:`create` 时原样传回。"""
        return {}

    def run(self, ctx: StageContext, inputs: In) -> Out:
        raise NotImplementedError


def agent_requires(options: JobOptions) -> list[str]:
    """整理与追问阶段的能力：取决于选用的 Agent 预设或内置大模型。"""
    if options.agent.preset:
        return [f"agent:{options.agent.preset}"]
    if options.agent.command:
        return ["agent:custom", "hub"]  # 自定义命令只在 Hub 所在电脑上配置
    return ["llm-api"]


def agent_model(options: JobOptions) -> str:
    if not (options.agent.preset or options.agent.command):
        return options.llm.model
    return " ".join(part for part in (options.agent.preset or "custom", options.agent.label) if part)

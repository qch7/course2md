"""阶段名 → 阶段类。节点按任务中的阶段名创建阶段，执行器按阶段名查调度声明。

新增阶段只需在 :data:`_STAGES` 中登记其类，其余信息由阶段类自己声明。
"""

from __future__ import annotations

from typing import Any

from .base import BaseStage
from .chat import ChatStage
from .cleanup import CleanupStage
from .frames import FramesStage
from .media import MediaStage
from .organize import OrganizeStage
from .output import OutputStage
from .proofread import ProofreadStage
from .sessions import SessionsStage

StageType = type[BaseStage[Any, Any]]

_STAGES: tuple[StageType, ...] = (
    MediaStage,
    FramesStage,
    ProofreadStage,
    OutputStage,
    OrganizeStage,
    ChatStage,
    CleanupStage,
    SessionsStage,  # 不属于主流程：由 Hub 在解析来源时单独派发
)

STAGE_TYPES: dict[str, StageType] = {stage.name: stage for stage in _STAGES}
if len(STAGE_TYPES) != len(_STAGES):
    raise RuntimeError("阶段名重复")

# 可以从中途重新运行的主流程阶段，按执行顺序
PIPELINE_STAGES: tuple[str, ...] = ("media", "frames", "proofread", "output", "organize")


def stage_type(name: str) -> StageType:
    try:
        return STAGE_TYPES[name]
    except KeyError:
        raise ValueError(f"未知阶段：{name}") from None


def create_stage(name: str, **resources: Any) -> BaseStage[Any, Any]:
    """resources 为节点上可复用的资源，由阶段的 ``create`` 自行挑选（如 media 的 backend）。"""
    return stage_type(name).create(**resources)


def stage_label(name: str) -> str:
    stage = STAGE_TYPES.get(name)
    return stage.display_name() if stage else name


STAGE_LABELS: dict[str, str] = {name: stage.display_name() for name, stage in STAGE_TYPES.items()}

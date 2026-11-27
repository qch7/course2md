"""按阶段名查阶段类声明的调度信息：缓存键、所需节点能力、生成模型。

这些函数要查阶段注册表，而注册表导入各阶段，各阶段又用到 :mod:`.codec` 的编解码，
所以不放在 codec 里，避免用函数内导入绕开循环。
"""

from __future__ import annotations

import hashlib
from typing import Any

from ..frame_selectors.api import DEFAULT_SELECTOR
from ..storage import canonical_json
from .options import JobOptions
from .registry import STAGE_TYPES


def cache_key(stage: str, options: dict[str, Any], inputs: Any) -> str:
    """相同阶段、相同输入与相同的相关选项得到相同的键，可复用之前的输出。"""
    stage_type = STAGE_TYPES.get(stage)
    keys = stage_type.cache_options if stage_type else None
    relevant = {key: value for key, value in options.items() if keys is None or key in keys}
    # 旧任务缺少策略字段，使用的也是课堂筛选。
    if relevant.get("frame_selector") == DEFAULT_SELECTOR:
        relevant.pop("frame_selector")
    return hashlib.sha256(canonical_json({"stage": stage, "options": relevant, "inputs": inputs})).hexdigest()


def requires_for(stage: str, options: JobOptions, inputs: Any = None) -> list[str]:
    """执行该阶段所需的节点能力，由阶段类声明。"""
    stage_type = STAGE_TYPES.get(stage)
    return list(stage_type.requires(options, inputs)) if stage_type else []


def output_model(stage: str, options: JobOptions) -> str:
    """记录在产物上的生成模型，由阶段类声明。"""
    stage_type = STAGE_TYPES.get(stage)
    return stage_type.model(options) if stage_type else ""

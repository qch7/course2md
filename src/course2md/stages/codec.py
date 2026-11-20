"""阶段任务的序列化：任务选项、阶段输入输出、缓存键与所需能力。

阶段交给队列执行时，选项与输入被编码成 JSON 存进任务；节点据此还原并执行，输出同样编码后保存为产物。
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..frame_selectors.api import DEFAULT_SELECTOR
from ..storage import canonical_json
from .artifacts import ARTIFACT_TYPES, load_artifact

if TYPE_CHECKING:
    from .options import JobOptions


def options_to_dict(options: JobOptions) -> dict[str, Any]:
    data = dataclasses.asdict(options)
    data["out_dir"] = str(options.out_dir)
    data["formats"] = list(options.formats)
    data["llm"].pop("api_key", None)  # 密钥留在执行节点本机，由节点从环境读取
    return data


def options_from_dict(data: dict[str, Any]) -> JobOptions:
    from ..agents import AgentConfig
    from ..config import AsrConfig, LlmConfig
    from .options import JobOptions

    values = dict(data)
    values["out_dir"] = Path(values["out_dir"])
    values["formats"] = tuple(values.get("formats") or ())
    values["asr"] = AsrConfig(**values.get("asr", {}))
    values["llm"] = LlmConfig(**values.get("llm", {}))
    values["agent"] = AgentConfig(**values.get("agent", {}))
    known = {item.name for item in dataclasses.fields(JobOptions)}
    return JobOptions(**{key: value for key, value in values.items() if key in known})


def encode(value: Any) -> Any:
    """阶段输入输出 → JSON。支持产物、产物列表与 None。"""
    if value is None:
        return None
    if isinstance(value, list):
        return [encode(item) for item in value]
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"无法编码阶段数据：{type(value).__name__}")


def decode(data: Any) -> Any:
    if data is None:
        return None
    if isinstance(data, list):
        return [decode(item) for item in data]
    if isinstance(data, dict) and data.get("kind") in ARTIFACT_TYPES:
        return load_artifact(data)
    raise TypeError("无法解码阶段数据")


def cache_key(stage: str, options: dict[str, Any], inputs: Any) -> str:
    """相同阶段、相同输入与相同的相关选项得到相同的键，可复用之前的输出。"""
    from .registry import STAGE_TYPES

    stage_type = STAGE_TYPES.get(stage)
    keys = stage_type.cache_options if stage_type else None
    relevant = {key: value for key, value in options.items() if keys is None or key in keys}
    # 旧任务缺少策略字段，使用的也是课堂筛选。
    if relevant.get("frame_selector") == DEFAULT_SELECTOR:
        relevant.pop("frame_selector")
    return hashlib.sha256(canonical_json({"stage": stage, "options": relevant, "inputs": inputs})).hexdigest()


def requires_for(stage: str, options: JobOptions, inputs: Any = None) -> list[str]:
    """执行该阶段所需的节点能力，由阶段类声明。"""
    from .registry import STAGE_TYPES

    stage_type = STAGE_TYPES.get(stage)
    return list(stage_type.requires(options, inputs)) if stage_type else []


def output_model(stage: str, options: JobOptions) -> str:
    """记录在产物上的生成模型，由阶段类声明。"""
    from .registry import STAGE_TYPES

    stage_type = STAGE_TYPES.get(stage)
    return stage_type.model(options) if stage_type else ""

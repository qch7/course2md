"""任务选项：一次任务的全部配置，随阶段任务序列化后交给节点（见 :mod:`.codec`）。

新增选项时同步检查：阶段的 ``cache_options`` 是否需要包含它、``codec.options_to_dict`` 是否需要脱敏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..agents import AgentConfig
from ..config import AsrConfig, LlmConfig
from ..frame_selectors.api import DEFAULT_SELECTOR


@dataclass
class JobOptions:
    """一次任务的配置。"""

    source: str
    out_dir: Path
    formats: tuple[str, ...] = ("srt", "txt", "md")
    session_id: str | None = None
    views: list[int] | None = None
    course: str = ""  # 课程名，用于输出目录分组与历史记录
    session_date: str = ""  # 课次日期 YYYY-MM-DD
    week: int | None = None  # 教学周次，仅用于展示与记录
    ai: bool = True
    proofread: bool = True
    detail: bool = True  # 详细笔记（默认）
    frames: bool = True  # 抽关键帧给 Agent 做画面参考
    frame_interval: float = 5.0  # 采样间隔（不是出图间隔）
    asr: AsrConfig = field(default_factory=AsrConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    keep_audio: bool = False
    delete_video: bool = False
    frame_selector: str = DEFAULT_SELECTOR

    @property
    def use_agent(self) -> bool:
        return self.ai and bool(self.agent.preset or self.agent.command)

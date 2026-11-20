"""流水线阶段。

- media：解析、下载、抽音频、转写。来源 → MediaFile、Transcript
- frames：全课扫描、筛选截图、索引。Transcript → FrameSet
- proofread：AI 校对。Transcript → Transcript
- output：写出各格式转写文件与元信息。Transcript → TranscriptFiles
- organize：课程记录与两版笔记。Transcript + FrameSet → Notes

编排见 :func:`course2md.pipeline.run_job`，执行位置由 :mod:`.executor` 决定。
"""

from .artifacts import ARTIFACT_VERSION, FrameSet, MediaFile, Notes, Transcript, TranscriptFiles, load_artifact
from .base import Cancelled, Stage, StageContext
from .executor import Executor, LocalExecutor
from .frames import FramesStage
from .media import MediaOutput, MediaStage, select_primary
from .organize import OrganizeInput, OrganizeStage
from .output import TRANSCRIPT_FORMATS, OutputStage, check_formats
from .proofread import ProofreadStage

__all__ = [
    "ARTIFACT_VERSION",
    "TRANSCRIPT_FORMATS",
    "Cancelled",
    "Executor",
    "FrameSet",
    "FramesStage",
    "LocalExecutor",
    "MediaFile",
    "MediaOutput",
    "MediaStage",
    "Notes",
    "OrganizeInput",
    "OrganizeStage",
    "OutputStage",
    "ProofreadStage",
    "Stage",
    "StageContext",
    "Transcript",
    "TranscriptFiles",
    "check_formats",
    "load_artifact",
    "select_primary",
]

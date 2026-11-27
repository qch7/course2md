"""流水线编排：来源 → 媒体 → 画面 → 校对 → 输出 → 整理。

各阶段的实现见 :mod:`course2md.stages`，本模块只负责按顺序把阶段交给执行器、
选择笔记主音轨、累计耗时、写出 job.json 并在成功后清理下载的视频。

所有阶段通过 progress 回调汇报 (阶段, 总进度 0-1, 说明, 子进度 0-1)。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .agents import AgentOutput
from .resolvers import public_source
from .stages import (
    Executor,
    FramesStage,
    LocalExecutor,
    MediaFile,
    MediaOutput,
    MediaStage,
    OrganizeInput,
    OrganizeStage,
    OutputStage,
    ProofreadStage,
    StageContext,
    check_formats,
    select_primary,
)
from .stages.base import Progress, noop_progress
from .stages.options import JobOptions
from .util.text import ensure_dir

__all__ = ["JobResult", "run_job", "run_stages"]


@dataclass
class JobResult:
    """任务产物。transcripts 为主音轨文件，transcripts_by_media 包含每路文件。"""

    output_dir: Path
    transcripts: dict[str, Path] = field(default_factory=dict)
    notes: Path | None = None
    segments: int = 0
    chars: int = 0
    corrected: int = 0
    backend: str = "builtin"
    warnings: list[str] = field(default_factory=list)
    primary_media: str = ""
    transcripts_by_media: dict[str, dict[str, Path]] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    detailed_notes: Path | None = None
    brief_notes: Path | None = None
    note_workspace: Path | None = None
    deleted_media: list[Path] = field(default_factory=list)


def run_stages(ctx: StageContext, executor: Executor, result: JobResult) -> MediaOutput:
    """按顺序把各阶段交给执行器，并把主音轨、转写统计和笔记路径写进 result。

    阶段之间的输入由这里推导。导入互传产物时，用同一段逻辑为各阶段重算缓存键
    （见 :mod:`course2md.jobs.stage_cache`），因此改动阶段顺序或输入时不会让两处漂移。
    """
    options = ctx.options
    timed = ctx.progress

    # 媒体：解析、下载、抽音频、转写
    media = executor.run(MediaStage(), ctx, None)
    transcripts = media.transcripts
    primary = select_primary(transcripts)
    result.primary_media = primary.stem
    if len(transcripts) > 1:
        timed("转写", 0.56, f"笔记主音轨：{primary.stem}，各机位转写分别保留", None)

    # 画面：需要原视频，与媒体阶段同一节点
    frame_sets = []
    if options.ai and options.frames:
        frame_sets = executor.run(FramesStage(), ctx, transcripts)

    # 校对
    if options.ai and options.proofread:
        transcripts = executor.run(ProofreadStage(), ctx, transcripts)
        primary = next(item for item in transcripts if item.stem == primary.stem)

    # 输出转写文件
    for item in transcripts:
        result.segments += len(item.segments)
        result.chars += sum(len(seg.text) for seg in item.segments)
        result.corrected += item.corrected
    for files in executor.run(OutputStage(), ctx, transcripts):
        result.transcripts_by_media[files.stem] = files.files
    result.transcripts = result.transcripts_by_media[primary.stem]

    # 整理笔记
    if options.ai:
        notes = executor.run(OrganizeStage(), ctx, OrganizeInput(primary, frame_sets))
        result.detailed_notes, result.brief_notes = notes.detailed, notes.brief
        result.notes = result.detailed_notes if options.detail else result.brief_notes
        result.note_workspace = notes.workspace
    return media


def run_job(
    options: JobOptions,
    progress: Progress = noop_progress,
    cancel: threading.Event | None = None,
    on_output: AgentOutput | None = None,
    executor: Executor | None = None,
) -> JobResult:
    """执行流水线。产物落盘后可清理本任务下载的视频，失败与取消保留输入视频。

    executor 决定阶段在哪里运行，默认在当前进程内执行。
    """
    check_formats(options.formats)
    executor = executor or LocalExecutor()
    result = JobResult(output_dir=options.out_dir, backend="agent" if options.use_agent else "builtin")
    timings: dict[str, float] = {}
    clock: dict[str, Any] = {"stage": "", "t0": time.perf_counter()}

    def timed(stage: str, fraction: float, message: str, sub: float | None = None) -> None:
        """包一层计时：按阶段累计耗时，最后写进 job.json。"""
        if stage != clock["stage"]:
            if clock["stage"]:
                timings[clock["stage"]] = timings.get(clock["stage"], 0.0) + time.perf_counter() - clock["t0"]
            clock["stage"], clock["t0"] = stage, time.perf_counter()
        progress(stage, fraction, message, sub)

    ctx = StageContext(
        options=options,
        progress=timed,
        cancel=cancel or threading.Event(),
        on_output=on_output,
        warnings=result.warnings,
    )
    ensure_dir(options.out_dir)

    media = run_stages(ctx, executor, result)

    if clock["stage"]:
        timings[clock["stage"]] = timings.get(clock["stage"], 0.0) + time.perf_counter() - clock["t0"]
    result.timings = {stage: round(seconds, 3) for stage, seconds in timings.items()}

    manifest = {
        "source": public_source(options.source),
        "course": options.course,
        "session_date": options.session_date,
        "week": options.week,
        "segments": result.segments,
        "chars": result.chars,
        "corrected": result.corrected,
        "backend": result.backend,
        "agent": options.agent.preset or ("custom" if options.agent.command else ""),
        "primary_media": result.primary_media,
        "transcripts": {k: str(v) for k, v in result.transcripts.items()},
        "transcripts_by_media": {
            stem: {fmt: str(path) for fmt, path in paths.items()} for stem, paths in result.transcripts_by_media.items()
        },
        "notes": str(result.notes) if result.notes else None,
        "detailed_notes": str(result.detailed_notes) if result.detailed_notes else None,
        "brief_notes": str(result.brief_notes) if result.brief_notes else None,
        "note_workspace": str(result.note_workspace) if result.note_workspace else None,
        "delete_video": options.delete_video,
        "frame_selector": options.frame_selector,
        "warnings": result.warnings,
        "timings": result.timings,
    }
    manifest_path = options.out_dir / "job.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ctx.check()
    if options.delete_video:
        cleanup = getattr(executor, "cleanup", None)
        if cleanup:
            cleanup(ctx, media)
        _delete_downloads(options.out_dir, media.files, result)
        manifest["deleted_media"] = [str(path) for path in result.deleted_media]
        manifest["warnings"] = result.warnings
        try:
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            result.warnings.append(f"笔记已保存，清理结果记录失败（{exc}）")
    timed(
        "完成",
        1.0,
        f"完成（{result.backend}）：{result.segments} 段 / {result.chars} 字 / 校对 {result.corrected} 处",
        None,
    )
    return result


def _delete_downloads(out_dir: Path, files: list[MediaFile], result: JobResult) -> None:
    """只删除本任务下载到 media 目录的视频，本地导入的文件不动。"""
    media_dir = out_dir / "media"
    local_imports = {item.path.resolve() for item in files if not item.remote}
    cleaned: set[Path] = set()
    for item in files:
        target = item.path.resolve()
        if not item.remote or target in local_imports or target in cleaned:
            continue
        if not target.is_relative_to(media_dir.resolve()) or not target.is_relative_to(out_dir.resolve()):
            result.warnings.append(f"视频清理路径超出下载目录，请检查 {item.path.name}")
            continue
        try:
            item.path.unlink(missing_ok=True)
            cleaned.add(target)
            result.deleted_media.append(item.path)
        except OSError as exc:
            result.warnings.append(f"视频清理失败，可手动删除 {item.path.name}（{exc}）")

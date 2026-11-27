"""互传产物包中的阶段缓存：导出时带上各阶段的输出，导入时在目标 Hub 登记成可复用的阶段记录。

没有这些记录时，目标 Hub 对导入的课次只能从头重跑（重新下载、转写、抽帧），
有了之后可以直接「从整理阶段重新运行」，例如只换 Agent 重新整理。

缓存键由阶段名、相关任务选项和阶段输入决定，而导入后的任务选项（任务目录、来源的公开形式等）与原 Hub 不同，
所以不能照搬对端的键，而是在目标 Hub 上按导入后的选项重新计算：用 :func:`course2md.pipeline.run_stages`
走一遍与真实运行相同的阶段编排，由 :class:`_SeedExecutor` 代替执行阶段，只做“算键并登记”。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..pipeline import JobResult, run_stages
from ..stages.artifacts import FrameSet, MediaFile, MediaOutput, Notes, Transcript, TranscriptFiles
from ..stages.base import StageContext
from ..stages.codec import decode, encode, options_to_dict
from ..stages.dispatch import cache_key, output_model
from ..stages.registry import PIPELINE_STAGES
from ..util.job_refs import from_job_refs, to_job_refs
from ..util.text import safe_name

if TYPE_CHECKING:
    from .job import Job
    from .manager import JobManager

IMPORTED_WORKER = "imported"


class _Incomplete(Exception):
    """某个阶段没有可登记的输出，后续阶段的输入也就无从得知。"""


def collect_stage_cache(manager: JobManager, job: Job) -> list[tuple[dict[str, Any], Any]]:
    """每个阶段最近一次成功的输出（路径已便携化、来源已脱敏）及其元信息，按流水线顺序。"""
    queue = getattr(manager, "queue", None)
    if queue is None or job.options is None:
        return []
    out_dir = job.out_dir.resolve()
    latest: dict[str, dict[str, Any]] = {}
    for task in queue.tasks_for_job(job.id):
        if task["status"] != "done" or not task["result"] or task["stage"] not in PIPELINE_STAGES:
            continue
        previous = latest.get(task["stage"])
        if previous is None or (task["finished_at"] or 0) >= (previous["finished_at"] or 0):
            latest[task["stage"]] = task
    items: list[tuple[dict[str, Any], Any]] = []
    for stage in PIPELINE_STAGES:
        task = latest.get(stage)
        if task is None:
            continue
        try:
            output = queue.output(task)
        except (OSError, ValueError, KeyError):
            continue
        meta = {
            "stage": stage,
            "model": output_model(stage, job.options),
            "warnings": list(task["warnings"]),
            "started_at": task["started_at"],
            "finished_at": task["finished_at"],
        }
        items.append((meta, _scrub(to_job_refs(output, out_dir))))
    return items


def _scrub(value: Any) -> Any:
    """媒体文件的本机路径与来源页地址不属于可分享的内容：改成任务目录内的占位，并去掉带链接的元信息。"""
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if not isinstance(value, dict):
        return value
    cleaned = {key: _scrub(item) for key, item in value.items()}
    if cleaned.get("kind") == "media-file":
        path = str(cleaned.get("path") or "")
        if not path.startswith("job://"):
            cleaned["path"] = "job://media/" + safe_name(Path(path.replace("\\", "/")).name, "media")
        cleaned["meta"] = {
            str(key): str(item)
            for key, item in (cleaned.get("meta") or {}).items()
            if key != "referer" and not str(item).lower().startswith(("http://", "https://"))
        }
    return cleaned


def _media_files(value: Any) -> Iterator[MediaFile]:
    if isinstance(value, MediaOutput):
        yield from value.files
        for item in value.transcripts:
            yield item.media
    elif isinstance(value, Transcript):
        yield value.media


def _paths(value: Any) -> Iterator[Path]:
    """产物引用的全部本地路径，用来确认导入的缓存只指向任务目录内。"""
    if isinstance(value, list):
        for item in value:
            yield from _paths(item)
    elif isinstance(value, FrameSet):
        yield from (frame.path for frame in value.frames)
        if value.index is not None:
            yield value.index
    elif isinstance(value, TranscriptFiles):
        yield from value.files.values()
        if value.meta_path is not None:
            yield value.meta_path
    elif isinstance(value, Notes):
        yield value.detailed
        yield value.brief
        if value.workspace is not None:
            yield value.workspace
    else:
        yield from (media.path for media in _media_files(value))


class _SeedExecutor:
    """不执行阶段，只用已有输出按真实运行的方式算出缓存键并登记。"""

    def __init__(self, manager: JobManager, job: Job, outputs: dict[str, tuple[dict[str, Any], Any]]) -> None:
        self.queue = manager.queue
        self.job = job
        self.outputs = outputs
        self.seeded: list[str] = []

    def run(self, stage: Any, ctx: StageContext, inputs: Any) -> Any:
        found = self.outputs.get(stage.name)
        if found is None:
            raise _Incomplete(stage.name)
        meta, output = found
        options = options_to_dict(ctx.options)
        encoded = encode(inputs)
        self.queue.record_hub_task(
            self.job.id,
            stage.name,
            {"options": options, "inputs": encoded, "run_id": ""},
            encode(output),
            cache_key=cache_key(stage.name, options, encoded),
            warnings=list(meta.get("warnings") or []),
            model=str(meta.get("model") or ""),
            owner_id=self.job.owner or None,
            started_at=_number(meta.get("started_at")),
            finished_at=_number(meta.get("finished_at")),
            worker_id=IMPORTED_WORKER,
        )
        self.seeded.append(stage.name)
        return output


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def restore_stage_cache(
    manager: JobManager, job: Job, entries: list[tuple[dict[str, Any], Any]], *, replace: bool = False
) -> tuple[list[str], list[str]]:
    """把包内的阶段输出登记为 job 的可复用记录，返回（已登记的阶段, 警告）。

    条目来自不可信的产物包：解码失败或引用了任务目录以外路径的阶段整体丢弃，只丢弃该阶段及其后的缓存，
    课次本身照常导入。replace 为 True 时先清掉该任务编号下原有的阶段记录，避免旧记录因时间更晚而被优先复用。
    """
    queue = getattr(manager, "queue", None)
    options = job.options
    if queue is None or options is None or not entries:
        return [], []
    root = job.out_dir.resolve()
    warnings: list[str] = []
    outputs: dict[str, tuple[dict[str, Any], Any]] = {}
    for meta, raw in entries:
        stage = str(meta.get("stage") or "")
        if stage not in PIPELINE_STAGES:
            continue
        try:
            output = decode(from_job_refs(raw, root))
            outside = next((path for path in _paths(output) if not path.resolve().is_relative_to(root)), None)
        except (ValueError, TypeError, KeyError, OSError):
            warnings.append(f"阶段缓存「{stage}」格式无效，已忽略")
            continue
        if outside is not None:
            warnings.append(f"阶段缓存「{stage}」引用了任务目录以外的路径，已忽略")
            continue
        outputs[stage] = (meta, output)
    if replace:
        queue.discard_job(job.id)
    executor = _SeedExecutor(manager, job, outputs)
    ctx = StageContext(options=options, cancel=threading.Event())
    try:
        run_stages(ctx, executor, JobResult(output_dir=root))
    except _Incomplete:
        pass
    except (RuntimeError, ValueError, KeyError, TypeError, StopIteration):
        warnings.append("阶段缓存不完整，已登记其中可用的部分")
    return executor.seeded, warnings

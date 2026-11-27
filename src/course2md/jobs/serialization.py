"""任务与数据库记录之间的转换。

存储层只认普通字典（见 :mod:`.workspace_store`），运行时对象里的锁、取消事件、缓存等不入库，
这里集中处理哪些字段保存、哪些在恢复时重建。新增 ``Job`` 字段时，若不该入库，在这里排除。
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

from ..pipeline import JobResult
from ..stages.codec import options_from_dict, options_to_dict
from .job import ACTIVE_STATUSES, Job, collect_files

# 运行时对象，不直接入库；其中 options / result / reuse 在下面单独转换
_NOT_STORED = ("cancel", "options", "result", "reuse")
_RESULT_PATHS = ("notes", "detailed_notes", "brief_notes", "note_workspace")


def job_to_record(job: Job) -> dict[str, Any]:
    """把任务转成可入库的字典；调用方应已持有 ``job.locked()``。"""
    data: dict[str, Any] = {
        item.name: getattr(job, item.name)
        for item in fields(Job)
        if not item.name.startswith("_") and item.name not in _NOT_STORED
    }
    data["reuse"] = sorted(job.reuse)
    data["cancel_requested"] = job.cancel.is_set()
    data["options"] = options_to_dict(job.options) if job.options else None
    data["result"] = (
        {name: str(getattr(job.result, name)) if getattr(job.result, name) else None for name in _RESULT_PATHS}
        if job.result
        else None
    )
    data["log_offset"], data["event_offset"] = job._log_count, job._event_seq
    return data


def job_from_record(data: dict[str, Any]) -> Job:
    """从数据库记录恢复任务；上次未结束的任务回到排队状态，等待接续。"""
    values = {
        item.name: data[item.name]
        for item in fields(Job)
        if item.name in data and not item.name.startswith("_") and item.name not in ("cancel", "options", "result")
    }
    values["out_dir"] = Path(values["out_dir"])
    values["reuse"] = frozenset(values.get("reuse") or ())
    job = Job(**values)
    if data.get("cancel_requested"):
        job.cancel.set()
    if data.get("options"):
        job.options = options_from_dict(data["options"])
    result = data.get("result")
    if result:
        paths = {name: Path(result[name]) if result.get(name) else None for name in _RESULT_PATHS}
        job.result = JobResult(
            output_dir=job.out_dir,
            timings=job.timings,
            notes=paths["notes"],
            detailed_notes=paths["detailed_notes"],
            brief_notes=paths["brief_notes"],
            note_workspace=paths["note_workspace"],
        )
    if job.out_dir.is_dir():
        job.files = collect_files(job.result or JobResult(output_dir=job.out_dir))
    job._log_count = data["log_offset"]
    job._event_seq = data["event_offset"]
    if job.status in ACTIVE_STATUSES:
        job.status, job.stage, job.message = "queued", "恢复", "服务已重启，正在接续未完成阶段"
        job.sub = None
    job.pending_messages = 0
    for message in job.messages:
        if message.get("status") in ACTIVE_STATUSES:
            message["status"] = "queued"
            if message["role"] == "user":
                job.pending_messages += 1
    return job

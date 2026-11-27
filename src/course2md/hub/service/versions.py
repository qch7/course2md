"""笔记版本历史的查询与恢复。"""

from __future__ import annotations

from ...core.accounts import LOCAL_USER, User
from ...jobs.job import ACTIVE_STATUSES
from ...jobs.manager import JobManager
from ..access import NotFound
from .common import get_job


def _versions(manager: JobManager):
    versions = getattr(manager, "versions", None)
    if versions is None:
        raise NotFound("没有版本记录")
    return versions


def note_versions(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> list[dict]:
    get_job(manager, job_id, user)
    versions = getattr(manager, "versions", None)
    return versions.list(job_id) if versions is not None else []


def note_version_text(manager: JobManager, job_id: str, version: int, kind: str, user: User = LOCAL_USER) -> dict:
    get_job(manager, job_id, user)
    if kind not in ("detailed", "brief"):
        raise ValueError("kind 可选 detailed、brief")
    text = _versions(manager).text(job_id, version, kind)
    if text is None:
        raise NotFound("版本不存在")
    return {"version": version, "kind": kind, "text": text}


def restore_note_version(manager: JobManager, job_id: str, version: int, user: User = LOCAL_USER) -> dict:
    """把两版笔记恢复为指定版本（作为新版本保存），配图沿用当前文件。"""
    job = get_job(manager, job_id, user)
    if job.status in ACTIVE_STATUSES or job.pending_messages:
        raise ValueError("课程正在处理，请稍后再恢复版本")
    if not job.result or not job.result.detailed_notes:
        raise ValueError("这个任务没有笔记文件")
    versions = _versions(manager)
    detailed = versions.text(job_id, version, "detailed")
    if detailed is None:
        raise NotFound("版本不存在")
    job.result.detailed_notes.write_text(detailed, encoding="utf-8", newline="")  # 按原样写回
    brief = versions.text(job_id, version, "brief")
    if brief is not None and job.result.brief_notes:
        job.result.brief_notes.write_text(brief, encoding="utf-8", newline="")
    with job.locked():
        job.revision += 1
        manager.record_version(job, f"恢复到第 {version} 版")
        manager.save_job(job)
    return {"job": job.snapshot(0)}

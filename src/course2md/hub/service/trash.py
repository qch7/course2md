"""回收站：软删除、恢复与永久删除。"""

from __future__ import annotations

import time
from typing import Any

from ...core.accounts import LOCAL_USER, User
from ...jobs.job import ACTIVE_STATUSES, Job
from ...jobs.manager import JobManager
from ...jobs.records import CourseRecord
from ...jobs.trash import TRASH_SECONDS
from ..access import NotFound, server_mode
from .common import get_course, get_job, request_cancel, visible


def delete_course(manager: JobManager, course_id: str, user: User = LOCAL_USER) -> dict:
    """移入回收站：课程及其全部课次立即从列表消失，进行中的任务先取消；30 天内可恢复。"""
    course = get_course(manager, course_id, user)
    now = time.time()
    for job in manager.trash.course_jobs(course):
        if job.deleted_at is not None:
            continue
        if job.status in ACTIVE_STATUSES:
            request_cancel(manager, job)
            job.log("课程已删除，任务已取消")
        manager.trash.mark_job_deleted(job, now)
    manager.trash.mark_course_deleted(course, now)
    return {"ok": True}


def delete_job(manager: JobManager, job_id: str, user: User = LOCAL_USER) -> dict:
    job = get_job(manager, job_id, user)
    if job.status in ACTIVE_STATUSES:
        request_cancel(manager, job)
        job.log("任务已删除，已取消")
    manager.trash.mark_job_deleted(job, time.time())
    return {"ok": True}


def list_trash(manager: JobManager, user: User = LOCAL_USER) -> dict:
    with manager.locked():
        courses = [c for c in manager.courses.values() if c.get("deleted_at") and visible(user, c.get("owner"))]
        jobs = [j for j in manager.jobs.values() if j.deleted_at is not None and visible(user, j.owner)]
    hidden = {course["id"] for course in courses}
    items: list[dict[str, Any]] = [
        {
            "kind": "course",
            "id": course["id"],
            "title": course.get("title") or "课程",
            "deleted_at": course.get("deleted_at") or 0.0,
            "purge_at": (course.get("deleted_at") or 0.0) + TRASH_SECONDS,
            "job_count": sum(1 for job in jobs if job.course_id == course["id"]),
        }
        for course in courses
    ]
    # 所属课程仍在列表中的单个课次，单独出现在回收站
    items += [
        {
            "kind": "job",
            "id": job.id,
            "title": job.title or job.course or "课次",
            "course": job.course,
            "deleted_at": job.deleted_at,
            "purge_at": (job.deleted_at or 0) + TRASH_SECONDS,
            "job_count": 1,
        }
        for job in jobs
        if job.course_id not in hidden
    ]
    items.sort(key=lambda item: item["deleted_at"], reverse=True)
    return {
        "items": items,
        "retention_days": TRASH_SECONDS // 86400,
        # 本地应用的输出目录是用户自己的，永久删除只清理记录
        "removes_files": server_mode(manager),
    }


def _trashed(manager: JobManager, kind: str, item_id: str, user: User) -> Job | CourseRecord:
    if kind == "course":
        course = get_course(manager, item_id, user, include_deleted=True)
        if not course.get("deleted_at"):
            raise NotFound("回收站中没有这个课程")
        return course
    if kind == "job":
        job = get_job(manager, item_id, user, include_deleted=True)
        if job.deleted_at is None:
            raise NotFound("回收站中没有这个课次")
        return job
    raise NotFound("未知的回收站项目")


def restore_trash(manager: JobManager, kind: str, item_id: str, user: User = LOCAL_USER) -> dict:
    item = _trashed(manager, kind, item_id, user)
    if isinstance(item, Job):
        manager.trash.mark_job_deleted(item, None)
    else:
        manager.trash.mark_course_deleted(item, None)
        for job in manager.trash.course_jobs(item):
            if job.deleted_at is not None:
                manager.trash.mark_job_deleted(job, None)
    return {"ok": True}


def purge_trash(manager: JobManager, kind: str, item_id: str, user: User = LOCAL_USER) -> dict:
    """立即永久删除，不再等待保留期。"""
    item = _trashed(manager, kind, item_id, user)
    if isinstance(item, Job):
        manager.trash.purge_job(item)
    else:
        manager.trash.purge_course(item)
    return {"ok": True}

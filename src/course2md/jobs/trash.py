"""回收站：软删除、恢复、超期永久删除与孤立对象回收。"""

from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import default_out_dir
from .job import ACTIVE_STATUSES, Job
from .records import CourseRecord

if TYPE_CHECKING:
    from .manager import JobManager

TRASH_SECONDS = 30 * 24 * 3600  # 回收站保留期
PURGE_INTERVAL = 3600.0


class TrashService:
    """管理器的回收站职责。任务与课程表、存储都属于管理器，这里只实现删除相关的流程。"""

    def __init__(self, manager: JobManager, closing: threading.Event) -> None:
        self._manager = manager
        self._closing = closing
        self._janitor: threading.Thread | None = None

    def start(self) -> None:
        """启动后台清理线程，随管理器退出。"""
        self._janitor = threading.Thread(target=self._purge_loop, daemon=True)
        self._janitor.start()

    def mark_job_deleted(self, job: Job, when: float | None) -> None:
        """移入（when 为时间戳）或移出（None）回收站。"""
        with job.locked():
            job.deleted_at = when
        self._manager.save_job(job)

    def mark_course_deleted(self, course: CourseRecord, when: float | None) -> None:
        manager = self._manager
        with manager.locked():
            if when is None:
                course.pop("deleted_at", None)
            else:
                course["deleted_at"] = when
            manager.store.save_course(course)

    def course_jobs(self, course: CourseRecord) -> list[Job]:
        manager = self._manager
        with manager.locked():
            return [
                job
                for job in manager.jobs.values()
                if job.course_id == course["id"] and job.owner == course.get("owner")
            ]

    def purge_job(self, job: Job) -> None:
        """永久删除任务：数据库记录、阶段记录与版本；服务器形态下还删除其输出目录。

        本地应用的输出目录由用户自己选择（可能是知识库），只清理记录，不删除用户磁盘上的文件。
        """
        manager = self._manager
        with manager.locked():
            manager.jobs.pop(job.id, None)
        manager.store.delete_job(job.id)
        manager.queue.discard_job(job.id)
        manager.versions.discard(job.id)
        if manager.mode == "server":
            self._remove_output(job.out_dir)

    def purge_course(self, course: CourseRecord) -> None:
        manager = self._manager
        for job in self.course_jobs(course):
            if job.status in ACTIVE_STATUSES:
                continue
            self.purge_job(job)
        with manager.locked():
            if self.course_jobs(course):
                return  # 仍有活动任务，稍后再清理
            manager.courses.pop(course["id"], None)
        manager.store.delete_course(course["id"])

    def purge_deleted(self, now: float | None = None, retention: float = TRASH_SECONDS) -> int:
        """永久删除超过保留期的回收站内容，返回清理的任务数。"""
        manager = self._manager
        cutoff = (time.time() if now is None else now) - retention
        with manager.locked():
            jobs = [
                job
                for job in manager.jobs.values()
                if job.deleted_at is not None and job.deleted_at <= cutoff and job.status not in ACTIVE_STATUSES
            ]
            courses = [
                course
                for course in manager.courses.values()
                if (deleted_at := course.get("deleted_at")) and deleted_at <= cutoff
            ]
        for job in jobs:
            self.purge_job(job)
        for course in courses:
            self.purge_course(course)
        return len(jobs)

    def prune_objects(self, grace_seconds: float = 3600.0, now: float | None = None) -> int:
        """回收对象存储中已无引用且超过宽限期的孤立对象（如已发布的临时上传块、已清除任务的产物）。"""
        return self._manager.queue.prune_unreferenced_objects(grace_seconds=grace_seconds, now=now)

    def _remove_output(self, out_dir: Path) -> None:
        """只删除服务器托管目录（输出根目录下的 users/）内的内容，并清理变空的课程文件夹。"""
        base = (Path(default_out_dir()).expanduser().resolve() / "users").resolve()
        target = Path(out_dir).resolve()
        if target == base or not target.is_relative_to(base):
            return
        shutil.rmtree(target, ignore_errors=True)
        parent = target.parent
        # 只清理变空的课程文件夹，保留 users/<用户编号> 这一层
        while parent.parent != base and parent.is_relative_to(base):
            try:
                parent.rmdir()  # 非空时抛出 OSError，保留
            except OSError:
                break
            parent = parent.parent

    def _purge_loop(self) -> None:
        while not self._closing.is_set():
            try:
                self.purge_deleted()
                self.prune_objects()
            except Exception as exc:  # noqa: BLE001  清理失败不应影响服务，下个周期重试
                self._manager.warnings.append(f"清理回收站失败（{exc}）")
            self._closing.wait(PURGE_INTERVAL)

"""接口无关的公共约定：错误类型、可见性与课程/任务查找。"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

from ...config import (
    default_out_dir,
)
from ...core.accounts import LOCAL_USER, User
from ...jobs.job import Job
from ...jobs.manager import JobManager
from ...jobs.records import CourseRecord
from ..access import NotFound, server_mode


def visible(user: User, owner: str | None) -> bool:
    """记录是否属于该用户（本地模式下全部属于本机用户 local）。"""
    return owner == user.id


def output_root(manager: JobManager, user: User, requested: str | None) -> Path:
    """服务器上每个用户一个目录，忽略请求中的路径；本地模式沿用请求或配置中的目录。"""
    base = Path(default_out_dir()).expanduser().resolve()
    if server_mode(manager):
        return base / "users" / user.id
    return Path(requested or base).expanduser().resolve()


def has_local_node(manager: JobManager) -> bool:
    """是否存在与 Hub 同机且可读取本机文件的在线节点（本地应用或 server --with-node）。"""
    queue = getattr(manager, "queue", None)
    if queue is not None:
        return queue.capable_worker_online(["resolver:local", "hub"])
    return getattr(manager, "builtin_worker", None) is not None


def get_job(manager: JobManager, job_id: str, user: User = LOCAL_USER, include_deleted: bool = False) -> Job:
    job = manager.get(job_id)
    if job is None or not visible(user, job.owner) or (job.deleted_at is not None and not include_deleted):
        raise NotFound("任务不存在")
    return job


def get_course(
    manager: JobManager, course_id: str, user: User = LOCAL_USER, include_deleted: bool = False
) -> CourseRecord:
    course = manager.courses.get(course_id)
    if course is None or not visible(user, course.get("owner")) or (course.get("deleted_at") and not include_deleted):
        raise NotFound("课程不存在")
    return course


def job_file(job: Job, rel: str) -> Path:
    """任务目录内的文件；越界或不存在时抛出 NotFound。"""
    target = (job.out_dir / unquote(rel)).resolve()
    if not target.is_relative_to(job.out_dir.resolve()) or not target.is_file():
        raise NotFound("文件不存在")
    return target


def request_cancel(manager: JobManager, job: Job) -> None:
    job.cancel.set()
    if getattr(manager, "queue", None):
        for task in manager.queue.tasks_for_job(job.id):
            manager.queue.cancel(task["id"])


IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")

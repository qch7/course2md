"""应用层：任务与课程的生命周期管理（提交、恢复、对话、版本、持久化）。

依赖方向：``hub`` / ``cli`` → ``jobs`` → ``pipeline`` / ``stages`` / ``core``。
本包不依赖任何 HTTP 框架，HTTP 接口见 :mod:`course2md.hub`。
"""

from .manager import ACTIVE_STATUSES, ConfirmationRequired, Job, JobManager
from .storage import WorkspaceStore

__all__ = ["ACTIVE_STATUSES", "ConfirmationRequired", "Job", "JobManager", "WorkspaceStore"]

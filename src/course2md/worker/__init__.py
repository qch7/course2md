"""计算节点：领取阶段任务、执行、回传产物。内置 Worker 运行在 Hub 进程中，独立节点见 ``course2md worker``。"""

from .capabilities import detect_capabilities, satisfies
from .link import HubLinkManager
from .runner import Transport, WorkerRunner

__all__ = ["HubLinkManager", "Transport", "WorkerRunner", "detect_capabilities", "satisfies"]

"""Hub 业务层共用的最基础约定：错误类型与运行形态判断。

单独成模块，是为了让 :mod:`course2md.hub.plugins` 与 :mod:`course2md.hub.service` 不必互相导入：
插件管理需要这些错误类型，而业务层的多个模块又需要插件管理检查“是否可以开始处理课程”。
"""

from __future__ import annotations

from typing import Any


class NotFound(LookupError):
    """请求的任务、课程或文件不存在（包括不属于当前用户的）。"""


class Forbidden(Exception):
    """当前用户无权执行该操作。

    不继承 PermissionError：它是 OSError 的子类，会被处理文件错误的 except 子句误吞。
    """


def server_mode(manager: Any) -> bool:
    """服务器形态：按用户隔离并限制可用操作。"""
    return getattr(manager, "mode", "local") == "server"

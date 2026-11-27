"""v1 各路由模块共用的依赖与参数校验。"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from ....core.accounts import User
from ....jobs.manager import JobManager
from ...auth import current_user

CurrentUser = Annotated[User, Depends(current_user)]


def get_manager(request: Request) -> JobManager:
    manager: JobManager = request.app.state.manager
    return manager


def non_negative(value: str, name: str) -> int:
    if not value.isdecimal() or len(value) > 15:
        raise ValueError(f"参数 {name} 必须是非负整数")
    return int(value)

"""REST 接口 v1。

全部路由位于 ``/api/v1`` 下，按领域拆在各子模块里，这里合并成一个路由。
破坏性改动只进入 v2。新增接口放进对应领域的模块；没有合适的领域时新建一个模块并在这里登记。
"""

from fastapi import APIRouter

from . import artifacts, compute, courses, jobs, system, transfer, versions

router = APIRouter()
for module in (system, courses, jobs, artifacts, versions, compute, transfer):
    router.include_router(module.router)

__all__ = ["router"]

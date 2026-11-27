"""测试里替换来源解析器工厂的辅助函数。

业务逻辑按领域拆在 ``course2md.hub.service`` 的子模块里，``get_resolver`` 被其中几个模块各自导入，
所以要在使用它的模块上逐个打补丁，而不是补在 ``service`` 包的导出上。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

RESOLVER_USERS = ("sources", "creation")


def patch_get_resolver(monkeypatch: pytest.MonkeyPatch, factory: Callable[[str], Any]) -> None:
    for module in RESOLVER_USERS:
        monkeypatch.setattr(f"course2md.hub.service.{module}.get_resolver", factory)

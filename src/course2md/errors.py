"""面向用户的错误基类与跨节点还原。

阶段在节点上失败时，Hub 只收到异常类名与消息。继承 :class:`Course2mdError` 的异常会自动登记，
Hub 端据此还原为同一类型，调用方可以照常 ``except ResolverError`` 等处理。

约定：
- 消息是给用户看的中文，说明原因与下一步，不含堆栈或密钥；
- 新的业务错误继承 :class:`Course2mdError`（或其子类），类名在整个包内唯一；
- 程序缺陷不要包装成业务错误，让它以原类型暴露。
"""

from __future__ import annotations

from typing import ClassVar

# 节点可能报告的标准异常，按原类型还原
_BUILTIN: dict[str, type[Exception]] = {"ValueError": ValueError, "RuntimeError": RuntimeError}


class Course2mdError(RuntimeError):
    """course2md 的业务错误基类。子类按类名登记，跨节点传递后可还原。"""

    registry: ClassVar[dict[str, type[Course2mdError]]] = {}

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        existing = Course2mdError.registry.get(cls.__name__)
        if existing is not None and existing is not cls and existing.__module__ != cls.__module__:
            raise TypeError(f"业务错误类名重复：{cls.__name__}（{existing.__module__} 与 {cls.__module__}）")
        Course2mdError.registry[cls.__name__] = cls


def rebuild_error(error_type: str, message: str) -> Exception | None:
    """按类名还原异常；未登记的类型返回 None，由调用方决定兜底类型。"""
    cls = Course2mdError.registry.get(error_type) or _BUILTIN.get(error_type)
    return cls(message) if cls is not None else None

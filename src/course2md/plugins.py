"""插件发现的公共实现：内置实现、Python 包入口点与环境变量指定的模块。

各扩展点（来源解析器、画面筛选策略、Agent 预设）共用同一套加载规则：

1. 内置实现优先，第三方插件不能覆盖内置名称；
2. 已安装包在入口点组（如 ``course2md.frame_selectors``）中登记的实现；
3. 可选的环境变量（如 ``COURSE2MD_RESOLVERS``）中以逗号分隔的 ``模块:属性``，便于开发时不安装直接加载；
4. 同一个非内置名称被多次声明时全部停用，避免按安装顺序随机选中；
5. 单个插件加载失败只记录原因（:meth:`PluginRegistry.errors`），不影响其它实现。

新增扩展点时创建一个 :class:`PluginRegistry`，不要再复制一份发现逻辑。
"""

from __future__ import annotations

import importlib
import os
import threading
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from importlib import metadata
from typing import Any, Generic, TypeVar

T = TypeVar("T")

BUILTIN_PACKAGE = "course2md"


@dataclass(frozen=True)
class Candidate:
    """一个待加载的实现。name 为入口点名；环境变量指定的模块在加载前不知道名称，记为空串。"""

    name: str
    origin: str  # 面向用户的来源说明
    package: str  # 内置为 course2md，环境变量为 env
    version: str
    load: Callable[[], Any]

    @property
    def builtin(self) -> bool:
        return self.package == BUILTIN_PACKAGE


def load_spec(spec: str) -> Any:
    """按 ``模块:属性.子属性`` 导入对象。"""
    module, _, attribute = spec.partition(":")
    if not module or not attribute:
        raise ValueError("格式应为 模块:属性")
    target: Any = importlib.import_module(module)
    for part in attribute.split("."):
        target = getattr(target, part)
    return target


def env_list(name: str) -> list[str]:
    """读取逗号分隔的环境变量。"""
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def discover(
    group: str,
    builtins: Mapping[str, str] | None = None,
    env_var: str | None = None,
    errors: list[str] | None = None,
) -> list[Candidate]:
    """按加载顺序返回候选实现：内置、入口点、环境变量。不加载对象本身。"""
    found = [
        Candidate(name, "builtin", BUILTIN_PACKAGE, "", partial(load_spec, spec))
        for name, spec in (builtins or {}).items()
    ]
    try:
        points = list(metadata.entry_points(group=group))
    except Exception as exc:  # noqa: BLE001  元数据损坏时仍保留内置实现
        if errors is not None:
            errors.append(f"读取插件列表 {group} 失败（{exc}）")
        points = []
    for point in points:
        dist = getattr(point, "dist", None)
        package = dist.name if dist else point.value.split(":", 1)[0]
        version = dist.version if dist else ""
        found.append(Candidate(point.name, f"入口点 {point.name}（{package}）", package, version, point.load))
    if env_var:
        for spec in env_list(env_var):
            found.append(Candidate("", f"{env_var} 中的 {spec}", "env", "", partial(load_spec, spec)))
    return found


class PluginRegistry(Generic[T]):
    """按名称加载实现的注册表，加载结果缓存到 :meth:`reload`。

    kind      面向用户的扩展点名称，用于错误信息（如“画面筛选策略”）
    group     入口点组名
    builtins  内置实现：名称 → ``模块:属性``
    validate  加载后校验对象，返回问题列表；为空表示通过
    """

    def __init__(
        self,
        kind: str,
        group: str,
        builtins: Mapping[str, str],
        validate: Callable[[Any], list[str]] | None = None,
        cached: bool = True,
    ) -> None:
        self.kind = kind
        self.group = group
        self.builtins = dict(builtins)
        self._validate = validate
        self._cached = cached  # False 时每次查询重新发现，运行中安装的插件立即可用
        self._lock = threading.Lock()
        self._loaded: dict[str, T] = {}
        self._errors: list[str] = []
        self._candidates: dict[str, Candidate] | None = None

    def _index(self) -> dict[str, Candidate]:
        with self._lock:
            if self._candidates is None or not self._cached:
                self._errors.clear()
                candidates = discover(self.group, self.builtins, errors=self._errors)
                counts = Counter(item.name for item in candidates if not item.builtin)
                index: dict[str, Candidate] = {}
                for item in candidates:
                    if item.builtin:
                        index[item.name] = item
                    elif item.name in self.builtins:
                        self._errors.append(f"{self.kind} {item.origin} 未启用：不能覆盖内置名称「{item.name}」")
                    elif counts[item.name] > 1:
                        self._errors.append(f"{self.kind} {item.origin} 未启用：多个插件声明了名称「{item.name}」")
                    else:
                        index[item.name] = item
                self._candidates = index
            return self._candidates

    def names(self) -> list[str]:
        """全部可用名称：内置在前，插件按名称排序。"""
        index = self._index()
        return [name for name in self.builtins if name in index] + sorted(
            name for name in index if name not in self.builtins
        )

    def plugin_names(self) -> list[str]:
        return [name for name in self.names() if name not in self.builtins]

    def get(self, name: str) -> T:
        """加载并返回名称对应的对象；未安装、重名或校验失败时抛出 ValueError。"""
        if self._cached and name in self._loaded:
            return self._loaded[name]
        candidate = self._index().get(name)
        if candidate is None:
            raise ValueError(f"未安装{self.kind}「{name}」，可选 {'、'.join(self.names()) or '无'}")
        try:
            target = candidate.load()
        except Exception as exc:
            raise ValueError(f"无法加载{self.kind} {name!r}：{type(exc).__name__}: {exc}") from exc
        problems = self._validate(target) if self._validate else []
        if problems:
            raise ValueError(f"{self.kind} {name!r} 不可用：{'；'.join(problems)}")
        if self._cached:
            self._loaded[name] = target
        return target

    def errors(self) -> list[str]:
        self._index()
        return list(self._errors)

    def reload(self) -> None:
        with self._lock:
            self._candidates = None
            self._loaded.clear()

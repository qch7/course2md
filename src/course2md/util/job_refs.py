"""在 Hub 与远程节点之间转换任务目录中的路径。

Hub 与节点各有自己的任务目录。交给节点前，payload 中位于 Hub 任务目录内的路径改写成
``job://相对路径``；节点把它们映射到本地任务目录，执行后再把本地路径改写回 ``job://``，
Hub 收到后映射回自己的目录。其它路径保持不变。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

SCHEME = "job://"


def _map_strings(value: Any, convert: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return convert(value)
    if isinstance(value, list):
        return [_map_strings(item, convert) for item in value]
    if isinstance(value, dict):
        return {key: _map_strings(item, convert) for key, item in value.items()}
    return value


def _relative(text: str, root: Path) -> str | None:
    """text 位于 root 内时返回 posix 形式的相对路径（root 本身为空串），否则 None。"""
    for flavour in (PureWindowsPath, PurePosixPath):
        try:
            path, base = flavour(text), flavour(str(root))
        except (TypeError, ValueError):
            continue
        if not path.is_absolute() or not base.is_absolute():
            continue
        parts, base_parts = path.parts, base.parts
        if flavour is PureWindowsPath:
            parts = tuple(part.lower() for part in parts)
            base_parts = tuple(part.lower() for part in base_parts)
        if parts[: len(base_parts)] == base_parts:
            return (
                PurePosixPath(*flavour(text).parts[len(base_parts) :]).as_posix()
                if len(parts) > len(base_parts)
                else ""
            )
    return None


def to_job_refs(value: Any, root: Path) -> Any:
    """root 内的路径 → job://相对路径。"""

    def convert(text: str) -> str:
        if text.startswith(SCHEME):
            return text
        relative = _relative(text, root)
        if relative is None:
            return text
        return SCHEME + relative

    return _map_strings(value, convert)


def from_job_refs(value: Any, root: Path) -> Any:
    """job://相对路径 → root 内的绝对路径。拒绝跳出 root 的相对路径。"""

    def convert(text: str) -> str:
        if not text.startswith(SCHEME):
            return text
        return str(resolve_relative(root, text[len(SCHEME) :]))

    return _map_strings(value, convert)


def resolve_relative(root: Path, relative: str) -> Path:
    parts = PurePosixPath(relative).parts
    if any(part in ("..", "") for part in parts) or PurePosixPath(relative).is_absolute() or ":" in relative:
        raise ValueError(f"非法的任务内路径：{relative!r}")
    return root.joinpath(*parts) if parts else root


def job_refs(value: Any) -> Iterator[str]:
    """payload 中出现的全部 job:// 相对路径。"""
    if isinstance(value, str):
        if value.startswith(SCHEME) and len(value) > len(SCHEME):
            yield value[len(SCHEME) :]
    elif isinstance(value, list):
        for item in value:
            yield from job_refs(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from job_refs(item)

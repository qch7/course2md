"""小型 JSON 状态文件的读写：节点、Hub 链接、对端 Hub 等配置都存成这种文件。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


def read_json_object(path: Path) -> dict[str, Any] | None:
    """读取顶层为对象的 JSON 文件；不存在、无法读取、不是合法 JSON 或顶层不是对象时返回 None。

    状态文件损坏不应该阻断启动，调用方按“没有已保存的配置”继续即可。
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def write_json_private(path: Path, payload: dict[str, Any], *, prefix: str = ".state-") -> None:
    """原子写入 JSON 文件，并在非 Windows 平台上收紧为仅所有者可读写。

    先写同目录的临时文件再替换，进程中途退出时不会留下写了一半的状态文件。文件里可能有令牌，
    所以不给其他用户读权限（Windows 上权限由目录 ACL 控制）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        if sys.platform != "win32":
            Path(tmp_name).chmod(0o600)
        os.replace(tmp_name, path)
    finally:
        Path(tmp_name).unlink(missing_ok=True)

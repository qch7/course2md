"""检查当前进程能否修改目录 ACL。

DeepSeek Harness 在 Windows 上执行命令前，会给工作目录添加沙箱身份的写权限。
course2md 若由另一个沙箱（例如自动化工具或 AI Agent 的受限终端）启动，
子进程继承受限令牌，授权会以 Win32 错误 5 失败，DSH 的命令工具因此全部不可用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from .proc import NO_WINDOW

ACL_FAILURE_MARKER = "windows-acl workspace grant failed"
ACL_HINT = (
    "DSH 无法为工作目录授权，命令工具不可用（Win32 错误 5）。course2md 可能是在受限的沙箱终端中启动的，"
    "请关闭当前服务，在普通 PowerShell 或资源管理器中运行 start.cmd 重新启动。"
    "无需给 DSH 开启完全权限。"
)


def acl_grant_problem(directory: Path) -> str | None:
    """在 directory 下建临时子目录并尝试授予写权限，失败时返回原因，非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    probe = Path(directory) / f".acl-probe-{uuid.uuid4().hex}"
    try:
        probe.mkdir(parents=True)
    except OSError as exc:
        return f"无法创建检查目录：{exc}"
    try:
        completed = subprocess.run(
            ["icacls", str(probe), "/grant", "*S-1-5-32-545:(OI)(CI)(W)"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"无法运行 icacls：{exc}"
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    if completed.returncode != 0:
        detail = completed.stdout.decode("mbcs", errors="replace").strip().splitlines()
        return detail[-1] if detail else f"icacls 退出码 {completed.returncode}"
    return None

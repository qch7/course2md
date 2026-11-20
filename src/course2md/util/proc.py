"""跨平台子进程工具：Windows 下不弹出控制台窗口，取消时终止整个进程树。

平台判断统一用 ``sys.platform == "win32"``，mypy 能据此在各平台收窄类型。
"""

from __future__ import annotations

import os
import subprocess
import sys

# 传给 subprocess 的 creationflags
NO_WINDOW: int = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def kill_tree(process: subprocess.Popen) -> None:
    """终止进程及其子进程，等待由调用者完成。POSIX 下要求以 ``start_new_session=True`` 启动。"""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
            timeout=10,
            check=False,
        )
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # 子进程组在终止请求到达前已经退出。

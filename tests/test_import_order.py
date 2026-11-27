"""每个入口模块都能在全新的进程里被第一个导入。

循环依赖常常只在某个特定的导入顺序下才暴露；换成函数内导入能绕开报错，却会把问题藏起来。
这里逐个从不同入口导入，保证模块之间没有依赖于导入顺序的循环。
"""

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

ENTRY_MODULES = [
    "course2md.stages.codec",
    "course2md.stages.dispatch",
    "course2md.stages.registry",
    "course2md.stages.executor",
    "course2md.hub.access",
    "course2md.hub.plugins",
    "course2md.hub.service",
    "course2md.hub.service.sources",
    "course2md.hub.app",
    "course2md.jobs.trash",
    "course2md.jobs.serialization",
    "course2md.jobs.manager",
    "course2md.worker.runner",
    "course2md.cli",
]


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_module_imports_first_in_a_fresh_process(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(SRC)!r}); import {module}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-800:]

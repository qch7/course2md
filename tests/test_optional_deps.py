"""核心模块在未安装 asr 可选依赖（faster-whisper、PyAV、OpenCV）时也能导入。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
BLOCK = """
import sys
class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {"faster_whisper", "av", "cv2", "ctranslate2"}:
            raise ImportError(f"blocked {name}")
        return None
sys.meta_path.insert(0, Blocker())
sys.path.insert(0, sys.argv[1])
"""


@pytest.mark.integration
@pytest.mark.parametrize(
    "module",
    [
        "course2md.pipeline",
        "course2md.stages",
        "course2md.jobs.manager",
        "course2md.cli",
        "course2md.refine.lesson",
        "course2md.config",
    ],
)
def test_core_imports_without_asr_extra(module):
    result = subprocess.run(
        [sys.executable, "-c", BLOCK + f"import {module}", str(SRC)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr

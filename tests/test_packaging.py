"""构建后的 wheel 包含前端、迁移脚本，并能启动 Hub。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


@pytest.mark.integration
def test_built_wheel_contains_web_page(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(
        root / "src" / "course2md", source / "src" / "course2md", ignore=shutil.ignore_patterns("__pycache__")
    )
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(root / name, source / name)
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    build = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from setuptools.build_meta import build_wheel; build_wheel(sys.argv[1])",
            str(wheel_dir),
        ],
        cwd=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    wheels = list(wheel_dir.glob("*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        if (root / "src" / "course2md" / "web_dist" / "index.html").is_file():
            assert "course2md/web_dist/index.html" in names
            assert any(name.startswith("course2md/web_dist/assets/") for name in names)
        assert "course2md/core/migrations/env.py" in names
        assert any(name.startswith("course2md/core/migrations/versions/0001_initial") for name in names)
        installed = tmp_path / "installed"
        archive.extractall(installed)
    smoke = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            """
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient

from course2md.hub.app import create_app

client = TestClient(create_app(None), base_url="http://127.0.0.1")
built = (Path(sys.argv[1]) / "course2md" / "web_dist" / "index.html").is_file()
response = client.get("/")
assert response.status_code == (200 if built else 503)
if built:
    assert b'id="app"' in client.get("/job/abc").content  # 前端的客户端路由
assert client.get("/api/v1/openapi.json").json()["info"]["title"] == "course2md"

import tempfile
from pathlib import Path

from course2md.core import Database

with tempfile.TemporaryDirectory() as folder:
    db = Database.at(Path(folder) / "hub.sqlite3")  # 迁移脚本随包发布
    db.dispose()
""",
            str(installed),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert smoke.returncode == 0, smoke.stdout + smoke.stderr

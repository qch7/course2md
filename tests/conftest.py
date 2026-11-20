"""固定课程样本，用于内容保留与导出回归。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


@pytest.fixture
def sample_course(tmp_path):
    data = json.loads((Path(__file__).parent / "fixtures" / "review-course.json").read_text(encoding="utf-8"))
    root = tmp_path / "course"
    workspace = root / "notes" / "agent-workspace" / "one"
    workspace.mkdir(parents=True)
    image = root / "frames" / "page.jpg"
    image.parent.mkdir()
    Image.new("RGB", (16, 16), "white").save(image)
    context = {
        "title": data["title"],
        "segments": data["segments"],
        "figures": {"f1": {"ts": 60, "path": "job://frames/page.jpg"}},
    }
    (workspace / "context.json").write_text(json.dumps(context), encoding="utf-8")
    (workspace / "lesson.json").write_text(json.dumps(data["lesson"]), encoding="utf-8")
    (root / "job.json").write_text(json.dumps({"note_workspace": str(workspace)}), encoding="utf-8")
    return SimpleNamespace(root=root, workspace=workspace, image=image, data=data)

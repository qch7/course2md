"""前端的托管方式与笔记版本历史。"""

from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from course2md.core import Database
from course2md.core.versions import NoteVersions
from course2md.hub.app import create_app
from course2md.jobs import manager as server
from course2md.storage import LocalFileStore


def _manager(root):
    manager = server.JobManager.__new__(server.JobManager)
    manager.jobs, manager.courses, manager.warnings = {}, {}, []
    manager._queue = queue.Queue()
    manager._lock = threading.RLock()
    manager.store = server.WorkspaceStore(root / "workspace.sqlite3")
    manager.versions = NoteVersions(manager.store.db, LocalFileStore(root / "objects"))
    manager.mode = "local"
    return manager


def test_spa_routes_fall_back_to_index(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
    (dist / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    client = TestClient(create_app(_manager(tmp_path), web_dist=dist), base_url="http://127.0.0.1")
    assert client.get("/").text == '<div id="app"></div>'
    assert client.get("/job/xyz").text == '<div id="app"></div>'
    assert client.get("/workers").status_code == 200
    asset = client.get("/assets/index-abc.js")
    assert asset.status_code == 200 and "immutable" in asset.headers["cache-control"]
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/api/v1/nowhere").json() == {"error": "未知路径"}


def test_without_build_the_root_explains_how_to_build(tmp_path):
    client = TestClient(create_app(_manager(tmp_path), web_dist=tmp_path / "missing"), base_url="http://127.0.0.1")
    response = client.get("/")
    assert response.status_code == 503 and "pnpm build" in response.text
    assert client.get("/job/xyz").status_code == 404


def test_versions_are_deduplicated(tmp_path):
    db = Database.at(tmp_path / "v.sqlite3")
    versions = NoteVersions(db, LocalFileStore(tmp_path / "objects"))
    detailed, brief = tmp_path / "a.notes.md", tmp_path / "a.brief.md"
    detailed.write_text("# 一\n", encoding="utf-8", newline="\n")
    brief.write_text("# 略\n", encoding="utf-8", newline="\n")
    assert versions.record("job", detailed, brief, "整理") == 1
    assert versions.record("job", detailed, brief, "对话") == 1
    detailed.write_text("# 二\n", encoding="utf-8", newline="\n")
    assert versions.record("job", detailed, brief, "对话：补充例题") == 2
    assert [item["version"] for item in versions.list("job")] == [2, 1]
    assert versions.text("job", 1, "detailed") == "# 一\n"
    assert versions.text("job", 9, "detailed") is None
    db.dispose()


@pytest.fixture
def versioned(tmp_path):
    manager = _manager(tmp_path)
    notes = tmp_path / "out" / "notes"
    notes.mkdir(parents=True)
    detailed, brief = notes / "a.notes.md", notes / "a.brief.md"
    detailed.write_text("# 第一版\n", encoding="utf-8", newline="\n")
    brief.write_text("# 略一\n", encoding="utf-8", newline="\n")
    job = server.Job(id="j", source="x", title="课", out_dir=tmp_path / "out", status="done", owner="local")
    job.result = SimpleNamespace(notes=detailed, detailed_notes=detailed, brief_notes=brief, note_workspace=None)
    manager.jobs["j"] = job
    manager._record_version(job, "整理")
    detailed.write_text("# 第二版\n", encoding="utf-8", newline="\n")
    manager._record_version(job, "对话：改写")
    client = TestClient(create_app(manager), base_url="http://127.0.0.1:8765")
    return SimpleNamespace(client=client, detailed=detailed, brief=brief, job=job)


def test_version_api_lists_reads_and_restores(versioned):
    client = versioned.client
    listed = client.get("/api/v1/jobs/j/versions").json()["versions"]
    assert [(item["version"], item["label"]) for item in listed] == [(2, "对话：改写"), (1, "整理")]
    assert client.get("/api/v1/jobs/j/versions/1", params={"kind": "brief"}).json()["text"] == "# 略一\n"
    assert client.get("/api/v1/jobs/j/versions/1", params={"kind": "other"}).status_code == 400
    assert client.get("/api/v1/jobs/j/versions/7").status_code == 404
    restored = client.post("/api/v1/jobs/j/versions/1/restore", json={})
    assert restored.status_code == 200, restored.text
    assert versioned.detailed.read_bytes() == "# 第一版\n".encode()
    assert next(item["version"] for item in client.get("/api/v1/jobs/j/versions").json()["versions"]) == 3
    versioned.job.status = "running"
    assert client.post("/api/v1/jobs/j/versions/2/restore", json={}).status_code == 400

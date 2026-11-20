"""Hub 接口 v1：版本化路径、OpenAPI、SSE 与错误格式。"""

from __future__ import annotations

import importlib.util
import json
import queue
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from course2md.hub.app import create_app
from course2md.jobs import manager as server

ROOT = Path(__file__).resolve().parents[1]


def _manager(root: Path):
    manager = server.JobManager.__new__(server.JobManager)
    manager.jobs, manager.courses, manager.warnings = {}, {}, []
    manager._queue = queue.Queue()
    manager._lock = threading.RLock()
    manager.store = server.WorkspaceStore(root / ".course2md" / "workspace.sqlite3")
    return manager


@pytest.fixture
def client(tmp_path):
    manager = _manager(tmp_path)
    job = server.Job(id="probe", source="local", title="probe", out_dir=tmp_path)
    manager.jobs[job.id] = job
    app = create_app(manager)
    app.state.event_interval = 0.01
    with TestClient(app, base_url="http://127.0.0.1:8765") as test_client:
        test_client.job = job
        yield test_client


def test_only_versioned_paths_exist(client):
    assert client.get("/api/v1/jobs/probe").json()["id"] == "probe"
    assert client.get("/api/v1/courses").json() == {"courses": []}
    assert client.get("/api/jobs/probe").status_code == 404


def test_openapi_lists_only_versioned_paths(client):
    schema = client.get("/api/v1/openapi.json").json()
    assert schema["paths"]
    assert all(path.startswith("/api/v1/") for path in schema["paths"])
    assert "CreateJobRequest" in schema["components"]["schemas"]


def test_committed_openapi_document_is_current():
    spec = importlib.util.spec_from_file_location("export_openapi", ROOT / "scripts" / "export_openapi.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (ROOT / "docs" / "api" / "openapi.json").read_text(encoding="utf-8") == module.render(), (
        "运行 python scripts/export_openapi.py"
    )


def test_errors_use_error_field_and_status(client):
    assert client.get("/api/v1/jobs/missing").status_code == 404
    assert client.get("/api/v1/jobs/missing").json() == {"error": "任务不存在"}
    assert client.get("/api/v1/nowhere").json() == {"error": "未知路径"}
    response = client.get("/api/v1/jobs/probe", params={"since": "-1"})
    assert response.status_code == 400 and "since" in response.json()["error"]
    response = client.post("/api/v1/jobs/probe/messages", json={"text": "  "})
    assert response.status_code == 400 and response.json()["error"] == "请填写要提问或修改的内容"
    response = client.post("/api/v1/jobs", content=b"[1]", headers={"Content-Type": "application/json"})
    assert response.status_code == 400 and response.json()["error"] == "请求内容必须是 JSON 对象"


def test_oversized_bodies_are_rejected(client):
    response = client.post(
        "/api/v1/jobs", content=b"{" + b" " * (1024 * 1024 + 1) + b"}", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400 and response.json()["error"] == "请求内容过大"


def test_sse_streams_job_updates_until_finished(client):
    job = client.job
    job.log("第一条")
    job.status = "running"

    def finish():
        job.log("第二条")
        job.status = "done"

    threading.Timer(0.05, finish).start()
    with client.stream("GET", "/api/v1/jobs/probe/events") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: ") :])))
                if name == "end":
                    break
    snapshots = [data for name, data in events if name == "job"]
    assert events[-1][0] == "end"
    assert snapshots[-1]["status"] == "done"
    logs = [line for snapshot in snapshots for line in snapshot["logs"]]
    assert sum("第一条" in line for line in logs) == 1  # 日志只增量推送
    assert any("第二条" in line for line in logs)

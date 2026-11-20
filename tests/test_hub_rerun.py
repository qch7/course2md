"""本地应用：任务经内置 Worker 执行，可查看阶段记录，并能只重新整理。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from course2md.asr.formats import Segment
from course2md.hub.app import create_app
from course2md.jobs import manager as server
from course2md.refine.lesson import NotePair
from course2md.resolvers.base import MediaRef
from course2md.stages import media as media_stage


@pytest.fixture
def local_app(tmp_path, monkeypatch):
    from course2md.refine import lesson

    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"video")
    calls = {"transcribe": 0, "notes": []}

    def transcribe(samples):
        calls["transcribe"] += 1
        return [Segment(0, 2, "讲话。")]

    def build_notes(segments, title, meta, llm, agent_config, *args, **kwargs):
        calls["notes"].append(agent_config.preset or "builtin")
        return NotePair(f"# 第 {len(calls['notes'])} 次\n", "# 缩略版\n")

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["fake-dsh"])
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["fake-codex"])
    monkeypatch.setattr(media_stage, "WhisperBackend", lambda cfg: SimpleNamespace(cfg=cfg, transcribe=transcribe))
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 60))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])
    monkeypatch.setattr(
        media_stage,
        "get_resolver",
        lambda source: SimpleNamespace(resolve=lambda *args: [MediaRef(str(video), "lecture.mp4", view=1)]),
    )
    monkeypatch.setattr(lesson, "build_note_pair", build_notes)
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(tmp_path / "out"))

    manager = server.JobManager(tmp_path / "out" / ".course2md" / "workspace.sqlite3")
    manager.builtin_worker.poll_interval = 0.02
    with TestClient(create_app(manager), base_url="http://127.0.0.1:8765") as client:
        client.calls = calls
        yield client
    manager.close()


def _wait(client, job_id, status="done", timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            assert job["status"] == status, job.get("error") or job["logs"][-3:]
            return job
        time.sleep(0.05)
    raise AssertionError("任务未在限定时间内结束")


def test_job_runs_through_builtin_worker_and_reruns_organize(local_app):
    client = local_app
    workers = client.get("/api/v1/workers").json()["workers"]
    assert workers[0]["id"] == "builtin" and workers[0]["online"]
    assert {"hub", "llm-api", "agent:dsh"} <= set(workers[0]["capabilities"])

    response = client.post(
        "/api/v1/jobs",
        json={"source": "lecture.mp4", "agent": "none", "proofread": False, "frames": False, "delete_video": False},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    _wait(client, job_id)
    tasks = client.get(f"/api/v1/jobs/{job_id}/tasks").json()["tasks"]
    assert [task["stage"] for task in tasks] == ["media", "output", "organize"]
    assert all(task["worker_id"] == "builtin" and "payload" not in task for task in tasks)

    response = client.post(f"/api/v1/jobs/{job_id}/rerun", json={"from_stage": "organize", "agent": "dsh"})
    assert response.status_code == 200, response.text
    job = _wait(client, job_id)
    assert client.calls["transcribe"] == 1
    assert client.calls["notes"] == ["builtin", "dsh"]
    assert job["agent"] == "dsh"
    tasks = client.get(f"/api/v1/jobs/{job_id}/tasks").json()["tasks"]
    assert [task["stage"] for task in tasks] == ["media", "output", "organize", "organize"]


def test_rerun_validates_stage_and_state(local_app):
    client = local_app
    job_id = client.post(
        "/api/v1/jobs", json={"source": "lecture.mp4", "agent": "none", "proofread": False, "frames": False}
    ).json()["id"]
    _wait(client, job_id)
    response = client.post(f"/api/v1/jobs/{job_id}/rerun", json={"from_stage": "render"})
    assert response.status_code == 400 and "from_stage" in response.json()["error"]
    assert client.post("/api/v1/jobs/missing/rerun", json={}).status_code == 404

"""「服务器 Hub + 个人电脑节点」：Hub 没有转写能力，远程节点经 HTTP 领取任务并回传文件。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import requests

from course2md.asr.formats import Segment
from course2md.frames.sampling import Frame
from course2md.hub.app import create_app
from course2md.jobs import manager as server
from course2md.refine.lesson import NotePair
from course2md.resolvers.base import MediaRef
from course2md.stages import media as media_stage
from course2md.util.job_refs import from_job_refs, to_job_refs
from course2md.worker.client import HttpTransport, HubError, RemoteWorker
from live_server import LiveServer


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    from course2md.frames import sampling as frames
    from course2md.refine import lesson

    node_dir = tmp_path / "node"
    seen = {}

    def resolve(*args):
        video = node_dir / "videos" / "lecture.mp4"  # 远程来源：由节点下载，Hub 上没有这个文件
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"video")
        return [MediaRef(str(video), "lecture.mp4", view=1)]

    def extract(media, out_dir, prefix, **kwargs):
        path = out_dir / f"{prefix}00-00-05.jpg"
        path.write_bytes(b"jpeg-from-node")
        return [Frame(5, path)]

    def build_notes(segments, title, *args, frames=None, workspace=None, **kwargs):
        seen["frames"] = [frame.path for frame in frames]
        seen["workspace"] = workspace
        return NotePair("# 详细版\n", "# 缩略版\n")

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", list)
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", list)
    monkeypatch.setattr(media_stage, "get_resolver", lambda source: SimpleNamespace(resolve=resolve))
    monkeypatch.setattr(
        media_stage,
        "WhisperBackend",
        lambda cfg: SimpleNamespace(cfg=cfg, transcribe=lambda samples: [Segment(0, 2, "节点上转写。")]),
    )
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 60))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])
    monkeypatch.setattr(frames, "extract", extract)
    monkeypatch.setattr(
        frames, "write_index", lambda frames, target, title: target.write_text("索引", encoding="utf-8") and target
    )
    monkeypatch.setattr(lesson, "build_note_pair", build_notes)
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(tmp_path / "hub"))

    # 纯 Hub（无内置节点）：output 由 Hub 自身落盘，全部重计算由远程节点完成
    manager = server.JobManager(tmp_path / "hub" / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    live = LiveServer(create_app(manager, bind_host="127.0.0.1"))
    base = f"http://127.0.0.1:{live.port}"
    worker_id, token = manager.queue.create_worker_token("个人电脑")
    node = RemoteWorker(
        HttpTransport(base, token),
        {"asr", "frames", "llm-api", "resolver:fakeclass"},
        node_dir,
        name="个人电脑",
        poll_interval=0.02,
    )
    node.start()
    yield SimpleNamespace(
        manager=manager,
        base=base,
        node=node,
        worker_id=worker_id,
        token=token,
        seen=seen,
        hub_dir=tmp_path / "hub",
        node_dir=node_dir,
    )
    node.stop()
    live.stop()
    manager.close()


def _wait(base, job_id, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = requests.get(f"{base}/api/v1/jobs/{job_id}", timeout=5).json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(0.05)
    raise AssertionError("任务未结束")


def test_remote_node_transcribes_and_files_return_to_hub(deployment):
    workers = {
        item["id"]: item for item in requests.get(f"{deployment.base}/api/v1/workers", timeout=5).json()["workers"]
    }
    assert workers[deployment.worker_id]["kind"] == "remote" and workers[deployment.worker_id]["online"]
    assert "builtin" not in workers

    response = requests.post(
        f"{deployment.base}/api/v1/jobs",
        timeout=10,
        json={"source": "fake://course/1", "agent": "none", "proofread": False, "frames": True, "delete_video": False},
    )
    assert response.status_code == 200, response.text
    job = _wait(deployment.base, response.json()["id"])
    assert job["status"] == "done", job["error"] or job["logs"][-5:]

    tasks = {
        task["stage"]: task
        for task in requests.get(f"{deployment.base}/api/v1/jobs/{job['id']}/tasks", timeout=5).json()["tasks"]
    }
    assert tasks["media"]["worker_id"] == tasks["frames"]["worker_id"] == deployment.worker_id
    assert tasks["organize"]["worker_id"] == deployment.worker_id
    assert tasks["output"]["worker_id"] == "hub"

    hub_job = deployment.hub_dir.joinpath(*job["out_dir"].split("hub", 1)[1].strip("\\/").replace("\\", "/").split("/"))
    assert (hub_job / "frames" / "lecture_00-00-05.jpg").read_bytes() == b"jpeg-from-node"
    assert (hub_job / "notes" / "lecture.notes.md").read_text(encoding="utf-8") == "# 详细版\n"
    assert "节点上转写" in (hub_job / "lecture.srt").read_text(encoding="utf-8")
    assert not (hub_job / "media").exists()  # 视频留在节点上
    node_job = deployment.node_dir / job["id"]
    assert all(str(path).startswith(str(node_job)) for path in deployment.seen["frames"])
    # 节点完成并回传后，临时下载/生成的 frames 与 notes 应立即清理，不长期堆积在节点上
    assert not (node_job / "frames").exists()
    assert not (node_job / "notes").exists()


def test_worker_endpoints_require_valid_token(deployment):
    bad = HttpTransport(deployment.base, "c2m_wrong")
    with pytest.raises(HubError, match="令牌"):
        bad.register_worker("", "x", [])
    assert requests.post(f"{deployment.base}/api/v1/worker/claim", json={}, timeout=5).status_code == 401
    requests.delete(
        f"{deployment.base}/api/v1/workers/{deployment.worker_id}",
        timeout=5,
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(HubError):
        HttpTransport(deployment.base, deployment.token).touch_worker("")


def test_token_api_creates_usable_tokens(deployment):
    response = requests.post(f"{deployment.base}/api/v1/workers", json={"name": "GPU 服务器"}, timeout=5)
    assert response.status_code == 200
    data = response.json()
    transport = HttpTransport(deployment.base, data["token"])
    transport.register_worker("", "GPU 服务器", ["asr"])
    assert transport.worker_id == data["worker_id"]
    assert data["token"] not in str(deployment.manager.queue.workers())


def test_job_paths_round_trip_between_directories(tmp_path):
    hub, node = tmp_path / "hub" / "课程" / "第1节", tmp_path / "node" / "abc"
    payload = {
        "options": {"out_dir": str(hub)},
        "inputs": [{"path": str(hub / "frames" / "a.jpg")}, {"path": "D:/elsewhere/x.mp4"}],
    }
    shared = to_job_refs(payload, hub)
    assert shared["options"]["out_dir"] == "job://"
    assert shared["inputs"][0]["path"] == "job://frames/a.jpg"
    assert shared["inputs"][1]["path"] == "D:/elsewhere/x.mp4"
    local = from_job_refs(shared, node)
    assert local["inputs"][0]["path"] == str(node / "frames" / "a.jpg")
    with pytest.raises(ValueError):
        from_job_refs({"x": "job://../escape"}, node)


def test_existing_worker_input_is_refreshed_by_digest(deployment, tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    source = root / "frame.jpg"
    source.write_bytes(b"old")
    queue = deployment.manager.queue
    queue.enqueue(
        "sync-job", "frames", {"options": {"out_dir": str(root)}, "inputs": {"path": str(source)}}, requires=["manual"]
    )
    transport = deployment.node.http
    task = transport.claim(deployment.worker_id, {"manual"})
    target = tmp_path / "download.jpg"
    assert transport.download(task.id, "frame.jpg", target)
    assert target.read_bytes() == b"old"
    before = target.stat().st_mtime_ns
    assert transport.download(task.id, "frame.jpg", target)
    assert target.stat().st_mtime_ns == before  # 304 不重复写入
    source.write_bytes(b"new")
    assert transport.download(task.id, "frame.jpg", target)
    assert target.read_bytes() == b"new"
    queue.fail(task.id, deployment.worker_id, "finished")
    with pytest.raises(HubError):
        transport.download(task.id, "frame.jpg", target)


def test_followup_runs_on_another_node_with_current_workspace(deployment, tmp_path, monkeypatch):
    import json

    from course2md.agents import AgentConfig
    from course2md.refine.conversation import Reply

    response = requests.post(
        f"{deployment.base}/api/v1/jobs",
        timeout=10,
        json={
            "source": "fake://course/1",
            "agent": "none",
            "proofread": False,
            "frames": True,
        },
    )
    snapshot = _wait(deployment.base, response.json()["id"])
    assert snapshot["status"] == "done"
    job = deployment.manager.get(snapshot["id"])
    job.options.agent = AgentConfig(preset="codex")  # Hub 未安装 Codex，追问必须派到远程节点。
    context = job.result.note_workspace / "context.json"
    context.parent.mkdir(parents=True, exist_ok=True)
    context.write_text(json.dumps({"updated-at-hub": True}), encoding="utf-8")
    deployment.node.stop()
    _worker_id, token = deployment.manager.queue.create_worker_token("第二台电脑")
    second = RemoteWorker(
        HttpTransport(deployment.base, token), {"agent:codex"}, tmp_path / "second", poll_interval=0.02
    )

    def reply(workspace, message, history, detailed, *_args):
        assert workspace.is_relative_to(second.work_dir)
        assert json.loads((workspace / "context.json").read_text(encoding="utf-8"))["updated-at-hub"]
        assert (second.work_dir / job.id / "frames" / "lecture_00-00-05.jpg").read_bytes() == b"jpeg-from-node"
        detailed.write_text("# 第二个节点更新\n", encoding="utf-8")
        return Reply("远程已更新", True)

    monkeypatch.setattr("course2md.refine.conversation.continue_notes", reply)
    second.start()
    try:
        response = requests.post(
            f"{deployment.base}/api/v1/jobs/{job.id}/messages", timeout=5, json={"id": "follow-up", "text": "补充例题"}
        )
        assert response.status_code == 200, response.text
        snapshot = _wait(deployment.base, job.id)
        assert snapshot["messages"][-1]["text"] == "远程已更新", snapshot
        tasks = deployment.manager.queue.tasks_for_job(job.id)
        assert tasks[-1]["stage"] == "chat" and tasks[-1]["worker_id"] == second.worker_id
        assert job.result.detailed_notes.read_text(encoding="utf-8") == "# 第二个节点更新\n"
    finally:
        second.stop()

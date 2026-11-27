"""互传产物包携带阶段缓存：导入后能从中间阶段重新运行，不必重新下载与转写。"""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from course2md.asr.formats import Segment
from course2md.hub.app import create_app
from course2md.jobs import manager as server
from course2md.jobs.bundle import export_bundle, import_bundle
from course2md.refine.lesson import NotePair
from course2md.resolvers import ResolverError
from course2md.resolvers.base import MediaRef
from course2md.stages import media as media_stage


@pytest.fixture
def hubs(tmp_path, monkeypatch):
    """两个互相独立的 Hub：来源 Hub 跑完一节课，目标 Hub 之后再也解析不了这个来源。"""
    from course2md.refine import lesson

    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"video")
    state = SimpleNamespace(transcribe=0, notes=[], resolvable=True)

    def transcribe(samples):
        state.transcribe += 1
        return [Segment(0, 2, "讲话。")]

    def build_notes(segments, title, meta, llm, agent_config, *args, **kwargs):
        state.notes.append(agent_config.preset or "builtin")
        return NotePair(f"# 第 {len(state.notes)} 次\n", "# 缩略版\n")

    def get_resolver(source):
        if not state.resolvable:
            raise ResolverError("没有能处理该来源的解析器")
        return SimpleNamespace(resolve=lambda *args: [MediaRef(str(video), "lecture.mp4", view=1)])

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["fake-dsh"])
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["fake-codex"])
    monkeypatch.setattr(media_stage, "WhisperBackend", lambda cfg: SimpleNamespace(cfg=cfg, transcribe=transcribe))
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 60))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])
    monkeypatch.setattr(media_stage, "get_resolver", get_resolver)
    monkeypatch.setattr(lesson, "build_note_pair", build_notes)
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(tmp_path / "src"))
    monkeypatch.setenv("COURSE2MD_API_KEY", "test-key")

    src = server.JobManager(tmp_path / "src" / ".course2md" / "workspace.sqlite3")
    dst = server.JobManager(tmp_path / "dst" / ".course2md" / "workspace.sqlite3")
    for manager in (src, dst):
        manager.builtin_worker.poll_interval = 0.02
    with (
        TestClient(create_app(src), base_url="http://127.0.0.1:8765") as src_client,
        TestClient(create_app(dst), base_url="http://127.0.0.1:8765") as dst_client,
    ):
        yield SimpleNamespace(src=src, dst=dst, src_client=src_client, dst_client=dst_client, state=state, tmp=tmp_path)
    src.close()
    dst.close()


def _wait(client, job_id, status="done", timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in ("done", "failed", "cancelled"):
            assert job["status"] == status, job.get("error") or job["logs"][-3:]
            return job
        time.sleep(0.05)
    raise AssertionError("任务未在限定时间内结束")


def _finished_job(hubs):
    response = hubs.src_client.post(
        "/api/v1/jobs",
        json={"source": "lecture.mp4", "agent": "none", "proofread": False, "frames": False, "delete_video": False},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    _wait(hubs.src_client, job_id)
    return hubs.src.jobs[job_id]


def _import(hubs, bundle: Path) -> str:
    result = import_bundle(hubs.dst, bundle, owner="local", root=hubs.tmp / "dst")
    assert len(result["imported"]) == 1, result
    return result["imported"][0]["id"]


def test_bundle_carries_stage_cache_only_when_asked(hubs):
    job = _finished_job(hubs)
    plain, cached = hubs.tmp / "plain.c2m.zip", hubs.tmp / "cached.c2m.zip"
    export_bundle(hubs.src, [job], plain)
    export_bundle(hubs.src, [job], cached, include_cache=True)

    with zipfile.ZipFile(plain) as archive:
        assert not any("/stages/" in name for name in archive.namelist())
    with zipfile.ZipFile(cached) as archive:
        names = set(archive.namelist())
        assert {f"jobs/job-0/stages/{stage}.json" for stage in ("media", "output", "organize")} <= names
        media = archive.read("jobs/job-0/stages/media.json").decode("utf-8")
        # 来源 Hub 上的本机视频路径不外泄，只留任务目录内的占位
        assert str(hubs.tmp / "lecture.mp4") not in media and "job://media/lecture.mp4" in media
        manifest = json.loads(archive.read("bundle.json"))
        assert [item["stage"] for item in manifest["jobs"][0]["stage_cache"]] == ["media", "output", "organize"]


def test_imported_stage_cache_lets_the_target_hub_rerun_from_organize(hubs):
    job = _finished_job(hubs)
    bundle = hubs.tmp / "cached.c2m.zip"
    export_bundle(hubs.src, [job], bundle, include_cache=True)
    new_id = _import(hubs, bundle)

    tasks = hubs.dst.queue.tasks_for_job(new_id)
    assert [task["stage"] for task in tasks] == ["media", "output", "organize"]
    assert {task["worker_id"] for task in tasks} == {"imported"}

    # 目标 Hub 解析不了来源：只有复用导入的缓存才能完成，转写也不会重做
    hubs.state.resolvable = False
    transcribed = hubs.state.transcribe
    response = hubs.dst_client.post(f"/api/v1/jobs/{new_id}/rerun", json={"from_stage": "organize", "agent": "dsh"})
    assert response.status_code == 200, response.text
    done = _wait(hubs.dst_client, new_id)
    assert done["agent"] == "dsh"
    assert hubs.state.transcribe == transcribed
    assert hubs.state.notes[-1] == "dsh"


def test_without_stage_cache_the_target_hub_has_to_start_over(hubs):
    job = _finished_job(hubs)
    bundle = hubs.tmp / "plain.c2m.zip"
    export_bundle(hubs.src, [job], bundle)
    new_id = _import(hubs, bundle)
    assert hubs.dst.queue.tasks_for_job(new_id) == []

    hubs.state.resolvable = False
    response = hubs.dst_client.post(f"/api/v1/jobs/{new_id}/rerun", json={"from_stage": "organize", "agent": "dsh"})
    assert response.status_code == 200, response.text
    failed = _wait(hubs.dst_client, new_id, status="failed")
    assert "解析器" in failed["error"]


def _rewrite_stage(bundle: Path, target: Path, stage: str, edit) -> None:
    """改写包内某阶段缓存并同步清单里的校验值，模拟恶意构造但自洽的产物包。"""
    with zipfile.ZipFile(bundle) as source:
        members = {name: source.read(name) for name in source.namelist()}
    name = f"jobs/job-0/stages/{stage}.json"
    data = edit(json.loads(members[name]))
    members[name] = (json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8")
    manifest = json.loads(members["bundle.json"])
    for item in manifest["jobs"][0]["stage_cache"]:
        if item["stage"] == stage:
            item["sha256"] = hashlib.sha256(members[name]).hexdigest()
    members["bundle.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    with zipfile.ZipFile(target, "w") as out:
        for member, payload in members.items():
            out.writestr(member, payload)


def test_stage_cache_pointing_outside_the_job_directory_is_ignored(hubs):
    job = _finished_job(hubs)
    bundle, forged = hubs.tmp / "cached.c2m.zip", hubs.tmp / "forged.c2m.zip"
    export_bundle(hubs.src, [job], bundle, include_cache=True)

    def point_outside(notes: dict) -> dict:
        notes["detailed"] = str(hubs.tmp / "elsewhere" / "secret.md")
        return notes

    _rewrite_stage(bundle, forged, "organize", point_outside)
    result = import_bundle(hubs.dst, forged, owner="local", root=hubs.tmp / "dst")
    assert len(result["imported"]) == 1
    assert any("任务目录以外" in message for message in result["warnings"])
    new_id = result["imported"][0]["id"]
    assert [task["stage"] for task in hubs.dst.queue.tasks_for_job(new_id)] == ["media", "output"]


def test_corrupted_stage_cache_rejects_the_bundle(hubs):
    job = _finished_job(hubs)
    bundle, broken = hubs.tmp / "cached.c2m.zip", hubs.tmp / "broken.c2m.zip"
    export_bundle(hubs.src, [job], bundle, include_cache=True)
    with zipfile.ZipFile(bundle) as source, zipfile.ZipFile(broken, "w") as out:
        for name in source.namelist():
            payload = source.read(name)
            out.writestr(name, payload + b" " if name.endswith("stages/media.json") else payload)
    with pytest.raises(ValueError, match="阶段缓存校验失败"):
        import_bundle(hubs.dst, broken, owner="local", root=hubs.tmp / "dst")
    assert not hubs.dst.jobs

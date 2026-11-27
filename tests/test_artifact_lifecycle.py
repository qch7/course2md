"""产物归属与生命周期：节点临时目录清理、媒体缓存 TTL、Hub 对象 GC 与缓存作用域隔离。"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from course2md.core import Database, TaskQueue
from course2md.core.versions import NoteVersions
from course2md.hub.app import create_app
from course2md.jobs.manager import Job, JobManager
from course2md.resolvers.base import MediaRef
from course2md.stages import media as media_stage
from course2md.stages.artifacts import MediaFile, MediaOutput
from course2md.stages.base import StageContext
from course2md.stages.cleanup import CleanupStage
from course2md.stages.options import JobOptions
from course2md.storage import LocalFileStore
from course2md.worker.client import RemoteWorker


def test_hub_object_gc_keeps_referenced_and_fresh_objects_and_prunes_stale(tmp_path):
    db = Database.at(tmp_path / "db.sqlite3")
    store = LocalFileStore(tmp_path / "objects")
    queue = TaskQueue(db, store)
    versions = NoteVersions(db, store)
    try:
        # 1) 被任务和产物引用的对象
        task_id = queue.enqueue("job-1", "proofread", {"options": {}, "inputs": None})
        queue.claim("w", set())
        task_digest = queue.complete(task_id, "w", {"kind": "demo"})

        # 2) 被笔记版本引用的对象
        detailed = tmp_path / "notes.md"
        brief = tmp_path / "brief.md"
        detailed.write_text("# 详细\n", encoding="utf-8")
        brief.write_text("# 简版\n", encoding="utf-8")
        versions.record("job-1", detailed, brief, "整理")

        # 3) 孤立的临时上传对象：一个过期、一个在宽限期内
        stale_blob = store.put_bytes(b"stale-upload-blob")
        fresh_blob = store.put_bytes(b"fresh-upload-blob")

        now = time.time()
        old_ts = now - 7200
        # 把所有对象的 mtime 调老（除了 fresh_blob），确保被引用的对象即使很老也不会被删
        for digest in (task_digest, stale_blob):
            os.utime(store.path(digest), (old_ts, old_ts))
        for digest in queue.referenced_digests():
            os.utime(store.path(digest), (old_ts, old_ts))

        removed = queue.prune_unreferenced_objects(grace_seconds=3600, now=now)
        assert removed == 1
        assert not store.exists(stale_blob)
        assert store.exists(fresh_blob)
        assert store.exists(task_digest)

        # 永久删除任务后，其旧任务与版本对象在宽限期过后也应被回收
        queue.discard_job("job-1")
        versions.discard("job-1")
        removed_after_discard = queue.prune_unreferenced_objects(grace_seconds=3600, now=now)
        assert removed_after_discard >= 3
        assert not store.exists(task_digest)
        assert store.exists(fresh_blob)
    finally:
        db.dispose()


def test_head_blob_touches_mtime_to_protect_inflight_upload(tmp_path):
    manager = JobManager(tmp_path / "workspace.sqlite3", builtin_worker=False)
    try:
        _wid, token = manager.queue.create_worker_token("node-1")
        digest = manager.queue.store.put_bytes(b"reused-frame-bytes")
        old_ts = time.time() - 7200
        os.utime(manager.queue.store.path(digest), (old_ts, old_ts))

        with TestClient(create_app(manager), base_url="http://127.0.0.1:8765") as client:
            res = client.head(f"/api/v1/worker/blobs/{digest}", headers={"Authorization": f"Bearer {token}"})
            assert res.status_code == 200

        # HEAD 刷新了 mtime，立即执行带宽限期的 GC 不得误删
        assert manager.prune_objects(grace_seconds=3600) == 0
        assert manager.queue.store.exists(digest)
    finally:
        manager.close()


def test_remote_worker_cleans_temp_files_and_prunes_stale_media_cache(tmp_path):
    node = RemoteWorker(Mock(), set(), tmp_path)

    # 1) _cleanup_temp 删掉临时下载/输出文件，保留非空 media/
    job_root = tmp_path / "job-a"
    (job_root / "media").mkdir(parents=True)
    (job_root / "frames").mkdir()
    (job_root / "notes" / "agent-workspace" / "w1").mkdir(parents=True)
    (job_root / "media" / "video.mp4").write_bytes(b"mp4")
    (job_root / "frames" / "f1.jpg").write_bytes(b"jpg")
    (job_root / "notes" / "lecture.notes.md").write_text("# note", encoding="utf-8")
    (job_root / "notes" / "agent-workspace" / "w1" / "context.json").write_text("{}", encoding="utf-8")

    node._cleanup_temp(job_root)
    assert (job_root / "media" / "video.mp4").is_file()
    assert not (job_root / "frames").exists()
    assert not (job_root / "notes").exists()

    # 2) CleanupStage 清掉 media 后，_cleanup_temp 会顺带移除空的任务目录
    CleanupStage().run(
        StageContext(JobOptions(source="x", out_dir=job_root)),
        MediaOutput(files=[MediaFile(path=job_root / "media" / "video.mp4", name="video.mp4", remote=True)]),
    )
    node._cleanup_temp(job_root)
    assert not job_root.exists()

    # 3) prune_cache 清理过期缓存与空目录，跳过新鲜缓存和正在执行的任务
    now = time.time()
    stale = tmp_path / "job-stale"
    (stale / "media").mkdir(parents=True)
    stale_file = stale / "media" / "old.mp4"
    stale_file.write_bytes(b"old")
    os.utime(stale_file, (now - 10 * 86400, now - 10 * 86400))

    fresh = tmp_path / "job-fresh"
    (fresh / "media").mkdir(parents=True)
    (fresh / "media" / "new.mp4").write_bytes(b"new")

    active = tmp_path / "job-active"
    (active / "media").mkdir(parents=True)
    active_file = active / "media" / "running.mp4"
    active_file.write_bytes(b"run")
    os.utime(active_file, (now - 10 * 86400, now - 10 * 86400))
    node._active_job_id = "job-active"

    empty = tmp_path / "job-empty"
    empty.mkdir()

    removed = node.prune_cache(max_age_seconds=7 * 86400, now=now)
    assert removed == 2
    assert not stale.exists()
    assert not empty.exists()
    assert fresh.exists()
    assert active.exists()


def test_media_stage_cleans_intermediate_wav_on_short_audio_and_failure(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        media_stage,
        "get_resolver",
        lambda source: SimpleNamespace(resolve=lambda *args: [MediaRef(str(video), "v.mp4", view=1)]),
    )

    def make_wav(_media, wav_path, duration):
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(b"wav")
        return wav_path, duration

    # 短音频跳过时也应清理 wav
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda m, w: make_wav(m, w, 2.0))
    monkeypatch.setattr(media_stage, "WhisperBackend", lambda cfg: SimpleNamespace(cfg=cfg, transcribe=lambda s: []))
    out_short = tmp_path / "out-short"
    media_stage.MediaStage().run(StageContext(JobOptions(source="x", out_dir=out_short, keep_audio=False)))
    assert not (out_short / "audio").exists()

    # 转写异常时也应清理 wav
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda m, w: make_wav(m, w, 60.0))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda w: [])
    monkeypatch.setattr(
        media_stage,
        "WhisperBackend",
        lambda cfg: SimpleNamespace(cfg=cfg, transcribe=lambda s: (_ for _ in ()).throw(RuntimeError("boom"))),
    )
    out_fail = tmp_path / "out-fail"
    with pytest.raises(RuntimeError, match="boom"):
        media_stage.MediaStage().run(StageContext(JobOptions(source="x", out_dir=out_fail, keep_audio=False)))
    assert not list((out_fail / "audio").glob("*.wav"))


def test_find_reusable_is_scoped_by_job_and_owner(tmp_path):
    db = Database.at(tmp_path / "db.sqlite3")
    queue = TaskQueue(db, LocalFileStore(tmp_path / "objects"))
    try:
        t1 = queue.enqueue("job-a", "proofread", {"options": {}, "inputs": None}, cache_key="same", owner_id="alice")
        queue.claim("w", set())
        queue.complete(t1, "w", {"ok": "alice"})

        assert queue.find_reusable("same", job_id="job-a", owner_id="alice")["id"] == t1
        assert queue.find_reusable("same", job_id="job-b", owner_id="alice") is None
        assert queue.find_reusable("same", job_id="job-a", owner_id="bob") is None
    finally:
        db.dispose()


def test_purge_job_allows_object_gc(tmp_path):
    manager = JobManager(tmp_path / "workspace.sqlite3", builtin_worker=False)
    try:
        job = Job("j1", "x", "x", tmp_path / "out")
        manager.jobs[job.id] = job
        manager.save_job(job)
        tid = manager.queue.enqueue(job.id, "proofread", {"options": {}, "inputs": None})
        manager.queue.claim("w", set())
        digest = manager.queue.complete(tid, "w", {"data": 1})
        assert manager.queue.store.exists(digest)

        manager.purge_job(job)
        assert manager.prune_objects(grace_seconds=0) >= 1
        assert not manager.queue.store.exists(digest)
    finally:
        manager.close()

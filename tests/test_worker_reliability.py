"""节点上传期间的租约与按内容同步回归。"""

import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from course2md.core import Database, TaskQueue
from course2md.core.models import TaskRow
from course2md.hub.app import create_app
from course2md.jobs.manager import JobManager
from course2md.pipeline import JobOptions
from course2md.stages.codec import options_to_dict
from course2md.storage import LocalFileStore
from course2md.worker import WorkerRunner
from course2md.worker.client import RemoteWorker


def test_lease_and_online_heartbeat_continue_during_slow_completion(tmp_path, monkeypatch):
    db = Database.at(tmp_path / "tasks.sqlite3")
    queue = TaskQueue(db, LocalFileStore(tmp_path / "objects"))
    renewed = threading.Event()
    uploading = threading.Event()
    original_renew, original_complete = queue.renew, queue.complete

    def renew(*args):
        result = original_renew(*args)
        if uploading.is_set():
            renewed.set()
        return result

    def complete(*args, **kwargs):
        uploading.set()
        assert renewed.wait(4), "上传时必须继续续约"
        return original_complete(*args, **kwargs)

    monkeypatch.setattr(queue, "renew", renew)
    monkeypatch.setattr(queue, "complete", complete)
    monkeypatch.setattr(
        "course2md.worker.runner.create_stage", lambda *a, **k: SimpleNamespace(run=lambda *a: None, resources=dict)
    )
    runner = WorkerRunner(queue, set(), worker_id="w", lease_seconds=3)
    runner.register()
    task = queue.enqueue(
        "job", "output", {"options": options_to_dict(JobOptions(source="x", out_dir=tmp_path)), "inputs": None}
    )
    try:
        assert runner.run_once()
        assert queue.get(task)["status"] == "done"
        assert queue.workers()[0]["online"]
    finally:
        db.dispose()


def test_modified_outputs_upload_even_with_unchanged_mtime(tmp_path):
    transport = Mock()
    node = RemoteWorker(transport, set(), tmp_path)
    file = tmp_path / "notes.md"
    file.write_text("old")
    old_time = file.stat().st_mtime
    node._before["t"] = node._files(tmp_path)
    file.write_text("new")
    os.utime(file, (old_time, old_time))
    assert "notes.md" in node.upload_outputs("t", tmp_path)
    transport.upload.assert_called_once()


def test_stopping_worker_returns_task_to_queue_for_restart(tmp_path, monkeypatch):
    db = Database.at(tmp_path / "tasks.sqlite3")
    queue = TaskQueue(db, LocalFileStore(tmp_path / "objects"))
    started = threading.Event()

    def run(ctx, _inputs):
        started.set()
        assert ctx.cancel.wait(4)
        ctx.check()

    monkeypatch.setattr(
        "course2md.worker.runner.create_stage", lambda *a, **k: SimpleNamespace(run=run, resources=dict)
    )
    task = queue.enqueue(
        "job",
        "output",
        {
            "options": options_to_dict(JobOptions(source="x", out_dir=tmp_path)),
            "inputs": None,
        },
    )
    runner = WorkerRunner(queue, set(), worker_id="w", poll_interval=0.02).start()
    try:
        assert started.wait(4)
        runner.stop()
        assert queue.get(task)["status"] == "pending"
        assert queue.claim("replacement", set()).id == task
    finally:
        runner.stop()
        db.dispose()


@pytest.mark.parametrize("reason", ["cancelled", "expired"])
def test_lost_lease_during_staging_cannot_overwrite_existing_notes(tmp_path, monkeypatch, reason):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    root = tmp_path / "course"
    target = root / "notes" / "course.notes.md"
    target.parent.mkdir(parents=True)
    target.write_text("保留现有笔记", encoding="utf-8")
    queue = manager.queue
    worker, token = queue.create_worker_token("test")
    task = queue.enqueue("job", "organize", {"options": {"out_dir": str(root)}})
    queue.claim(worker, set())
    digest = queue.store.put_bytes("已失效的回传笔记".encode())
    copy_to = queue.store.copy_to

    def lose_lease(digest, path):
        result = copy_to(digest, path)
        if reason == "cancelled":
            queue.cancel(task)
        else:
            with queue.db.session() as session:
                session.execute(update(TaskRow).where(TaskRow.id == task).values(lease_until=time.time() - 1))
        return result

    monkeypatch.setattr(queue.store, "copy_to", lose_lease)
    try:
        client = TestClient(create_app(manager), base_url="http://127.0.0.1")
        response = client.post(
            f"/api/v1/worker/tasks/{task}/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={"output": {}, "files": {"notes/course.notes.md": digest}},
        )
        assert response.status_code == 409
        assert target.read_text(encoding="utf-8") == "保留现有笔记"
        assert not list(root.parent.glob(".course2md-publish-*"))
    finally:
        manager.close()

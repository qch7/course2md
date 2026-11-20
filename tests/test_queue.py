"""阶段任务队列、产物存储与旧数据库升级。"""

from __future__ import annotations

import sqlite3
import time

import pytest
from sqlalchemy import update

from course2md.core import Database, LeaseLost, TaskQueue
from course2md.core.db import IncompatibleDatabase
from course2md.core.models import ArtifactRow, TaskRow, WorkerRow
from course2md.jobs.storage import WorkspaceStore
from course2md.storage import LocalFileStore, canonical_json, digest_bytes


@pytest.fixture
def queue(tmp_path):
    db = Database.at(tmp_path / "hub.sqlite3")
    yield TaskQueue(db, LocalFileStore(tmp_path / "objects"))
    db.dispose()


def test_databases_from_other_versions_are_rejected(tmp_path):
    path = tmp_path / "workspace.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE courses (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
    with pytest.raises(IncompatibleDatabase, match="早期版本"):
        Database.at(path)
    unknown = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(unknown) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('0004')")
    with pytest.raises(IncompatibleDatabase):
        Database.at(unknown)
    fresh = Database.at(tmp_path / "fresh.sqlite3")
    Database.at(tmp_path / "fresh.sqlite3").dispose()  # 再次打开当前版本的数据库正常
    fresh.dispose()


def test_job_records_keep_one_row_per_output_directory(tmp_path):
    store = WorkspaceStore(tmp_path / "w.sqlite3")
    assert store.save_job({"id": "a", "out_dir": "D:/x"})
    assert not store.save_job({"id": "b", "out_dir": "D:/x"})
    assert store.save_job({"id": "b", "out_dir": "D:/x"}, replace_existing=True)
    assert [job["id"] for job in store.load_jobs()] == ["b"]
    assert store.save_job({"id": "b", "out_dir": "D:/x", "title": "更新"})
    assert store.load_jobs()[0]["title"] == "更新"
    store.db.dispose()


def test_local_store_is_content_addressed(tmp_path):
    store = LocalFileStore(tmp_path / "objects")
    first = store.put_bytes(b"lesson")
    assert first == digest_bytes(b"lesson") == store.put_bytes(b"lesson")
    assert store.get_bytes(first) == b"lesson"
    with (tmp_path / "frame.jpg").open("wb") as handle:
        handle.write(b"jpeg" * 1000)
    with (tmp_path / "frame.jpg").open("rb") as handle:
        digest = store.put_stream(handle)
    assert store.copy_to(digest, tmp_path / "copy" / "frame.jpg").read_bytes() == b"jpeg" * 1000
    assert store.get_json(store.put_bytes(canonical_json({"b": 1, "a": "课"}))) == {"a": "课", "b": 1}
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")
    with pytest.raises(KeyError):
        store.get_bytes("0" * 64)


def test_claim_respects_capabilities_and_affinity(queue):
    media = queue.enqueue("job", "media", {"n": 1}, requires=["asr", "resolver:local"])
    frames = queue.enqueue("job", "frames", {"n": 2}, requires=["frames"], affinity="gpu-box")
    assert queue.claim("api-only", {"llm-api"}) is None
    assert queue.claim("laptop", {"frames"}) is None  # frames 绑定在 gpu-box
    task = queue.claim("gpu-box", {"asr", "frames", "resolver:local@1.0"})
    assert task.id == media and task.attempts == 1 and task.payload == {"n": 1}
    assert queue.claim("gpu-box", {"asr", "frames", "resolver:local"}).id == frames
    assert queue.claim("gpu-box", {"asr", "frames"}) is None


def test_incompatible_tasks_do_not_starve_a_capable_worker(queue):
    for _ in range(51):
        queue.enqueue("blocked", "media", {}, requires=["asr"])
    ready = queue.enqueue("ready", "organize", {}, requires=["llm-api"])
    assert queue.claim("llm", {"llm-api"}).id == ready


def test_complete_stores_output_and_provenance(queue):
    task_id = queue.enqueue("job", "organize", {}, cache_key="k1")
    queue.claim("w", set())
    output = {"kind": "notes", "version": 1, "detailed": "a.md", "brief": "b.md", "workspace": None}
    digest = queue.complete(task_id, "w", output, ["提示"], model="deepseek-chat")
    task = queue.get(task_id)
    assert task["status"] == "done" and task["result"] == digest and task["warnings"] == ["提示"]
    assert queue.output(task) == output
    assert digest == digest_bytes(canonical_json(output))
    with queue.db.session() as session:
        artifact = session.query(ArtifactRow).one()
        assert (artifact.kind, artifact.stage, artifact.worker_id, artifact.model) == (
            "notes",
            "organize",
            "w",
            "deepseek-chat",
        )
    assert queue.find_reusable("k1")["id"] == task_id
    assert queue.find_reusable("other") is None


def test_renew_reports_cancel_and_rejects_other_workers(queue):
    task_id = queue.enqueue("job", "media", {})
    queue.claim("w1", set())
    assert queue.renew(task_id, "w1") is False
    with pytest.raises(LeaseLost):
        queue.renew(task_id, "w2")
    queue.cancel(task_id)
    assert queue.renew(task_id, "w1") is True
    queue.fail(task_id, "w1", "任务已取消", "Cancelled", cancelled=True)
    assert queue.get(task_id)["status"] == "cancelled"


def test_failed_file_publication_does_not_mark_task_done(queue):
    task_id = queue.enqueue("job", "organize", {})
    queue.claim("worker", set())

    def fail():
        raise OSError("文件改名失败")

    with pytest.raises(OSError, match="文件改名失败"):
        queue.complete(task_id, "worker", {"ok": True}, publish=fail)
    assert queue.get(task_id)["status"] == "leased"
    with queue.db.session() as session:
        assert list(session.query(ArtifactRow)) == []
    queue.complete(task_id, "worker", {"ok": True})
    assert queue.get(task_id)["status"] == "done"


def test_cancel_pending_task_finishes_it(queue):
    task_id = queue.enqueue("job", "media", {})
    queue.cancel(task_id)
    assert queue.get(task_id)["status"] == "cancelled"
    assert queue.claim("w", set()) is None


def test_expired_leases_return_to_queue_until_attempts_run_out(queue):
    task_id = queue.enqueue("job", "media", {}, max_attempts=2)

    def expire():
        with queue.db.session() as session:
            session.execute(update(TaskRow).where(TaskRow.id == task_id).values(lease_until=time.time() - 1))

    queue.claim("w1", set())
    expire()
    retry = queue.claim("w2", set())
    assert retry.id == task_id and retry.attempts == 2
    with pytest.raises(LeaseLost):
        queue.renew(task_id, "w1")
    expire()
    assert queue.claim("w3", set()) is None
    task = queue.get(task_id)
    assert task["status"] == "failed" and task["error_type"] == "LeaseExpired"


def test_progress_and_events_are_incremental(queue):
    task_id = queue.enqueue("job", "media", {})
    queue.claim("w", set())
    queue.progress(task_id, "w", "转写", 0.4, "一半", 0.5)
    queue.add_event(task_id, {"type": "text", "text": "a"})
    queue.add_event(task_id, {"type": "text", "text": "b"})
    task = queue.get(task_id)
    assert task["progress"] == {"stage": "转写", "fraction": 0.4, "message": "一半", "sub": 0.5, "seq": 1}
    events = queue.events(task_id)
    assert [event["text"] for _, event in events] == ["a", "b"]
    assert [event["text"] for _, event in queue.events(task_id, events[0][0])] == ["b"]


def test_worker_registry_reports_online_capabilities(queue):
    queue.register_worker("gpu", "GPU 服务器", ["asr", "frames"], kind="remote", version="0.3")
    assert queue.capable_worker_online(["asr"])
    assert not queue.capable_worker_online(["agent:dsh"])
    assert not queue.capable_worker_online(["asr"], affinity="other")
    assert queue.workers()[0]["online"] is True


@pytest.mark.parametrize("disabled", [False, True])
def test_delete_worker_preserves_history_and_revokes_token(queue, disabled):
    worker_id, token = queue.create_worker_token("旧电脑")
    other_id, other_token = queue.create_worker_token("新电脑")
    task_id = queue.enqueue("job", "organize", {}, cache_key="notes")
    queue.claim(worker_id, set())
    digest = queue.complete(task_id, worker_id, {"notes": "课程笔记"})
    queue.enqueue("other-job", "organize", {}, affinity=other_id)
    if disabled:
        assert queue.disable_worker(worker_id)

    assert queue.delete_worker(worker_id)
    assert queue.authenticate_worker(token) is None
    assert queue.authenticate_worker(other_token)["id"] == other_id
    assert [worker["id"] for worker in queue.workers()] == [other_id]
    assert queue.get(task_id)["worker_id"] == worker_id
    assert queue.find_reusable("notes")["id"] == task_id
    assert queue.store.get_json(digest) == {"notes": "课程笔记"}
    with queue.db.session() as session:
        assert session.get(WorkerRow, worker_id) is None
        assert session.query(ArtifactRow).one().worker_id == worker_id
    assert not queue.delete_worker(worker_id)


def test_delete_worker_protects_builtin_node(queue):
    queue.register_worker("builtin", "内置节点", {"hub"})
    assert not queue.delete_worker("builtin")
    assert queue.workers()[0]["id"] == "builtin"

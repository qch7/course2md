"""真实 PostgreSQL 上的迁移、存储与租约竞争；每个测试使用独立 schema。

设置 COURSE2MD_TEST_DATABASE_URL 后运行 pytest tests/test_postgres.py。
测试账号需要创建和删除 schema 的权限，测试不会使用 public 中的业务表。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from course2md.core import Database, LeaseLost, TaskQueue
from course2md.core.models import ArtifactRow, TaskRow
from course2md.jobs.storage import WorkspaceStore
from course2md.storage import LocalFileStore

URL = os.environ.get("COURSE2MD_TEST_DATABASE_URL", "")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="未设置 COURSE2MD_TEST_DATABASE_URL")]


@pytest.fixture
def pg_db():
    schema = f"course2md_test_{uuid.uuid4().hex}"
    admin = create_engine(URL)
    db = None
    with admin.begin() as connection:
        connection.execute(CreateSchema(schema))
    try:
        url = make_url(URL)
        options = f"{url.query.get('options', '')} -csearch_path={schema}"
        db = Database(url.update_query_dict({"options": options}).render_as_string(hide_password=False))
        yield db
    finally:
        if db is not None:
            db.dispose()
        with admin.begin() as connection:
            connection.execute(DropSchema(schema, cascade=True))
        admin.dispose()


@pytest.fixture
def pg_queue(pg_db, tmp_path):
    return TaskQueue(pg_db, LocalFileStore(tmp_path / "objects"))


def test_postgres_workspace_and_queue(pg_db, pg_queue):
    store = WorkspaceStore(db=pg_db)
    assert store.save_job({"id": "job", "out_dir": "/data/course"})
    assert not store.save_job({"id": "other", "out_dir": "/data/course"})
    assert store.load_jobs() == [{"id": "job", "out_dir": "/data/course"}]
    task_id = pg_queue.enqueue("job", "media", {"options": {"source": "s"}}, requires=["asr"])
    claimed = pg_queue.claim("worker", {"asr"})
    assert claimed is not None and claimed.id == task_id
    output = {"kind": "transcript", "text": "课程内容"}
    digest = pg_queue.complete(task_id, "worker", output)
    assert pg_queue.output(pg_queue.get(task_id)) == output
    with pg_db.session() as session:
        assert session.scalars(select(ArtifactRow)).one().digest == digest
    reopened = Database(pg_db.url)
    try:
        assert TaskQueue(reopened, pg_queue.store).get(task_id)["status"] == "done"
    finally:
        reopened.dispose()


def test_postgres_concurrent_jobs_cannot_occupy_same_directory(pg_db):
    store = WorkspaceStore(db=pg_db)
    barrier = threading.Barrier(8)

    def save(index):
        barrier.wait(timeout=10)
        return store.save_job({"id": f"job{index}", "out_dir": "/data/course"})

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(save, range(8))).count(True) == 1
    assert len(store.load_jobs()) == 1


@pytest.mark.parametrize("task_count", [1, 12])
def test_postgres_concurrent_claims_are_exclusive(pg_queue, task_count):
    expected = {pg_queue.enqueue("job", "media", {}, requires=["asr"]) for _ in range(task_count)}
    barrier = threading.Barrier(8)

    def claim(worker):
        barrier.wait(timeout=10)
        tasks = []
        while task := pg_queue.claim(worker, {"asr"}):
            tasks.append(task.id)
        return tasks

    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = [task for tasks in pool.map(claim, [f"w{i}" for i in range(8)]) for task in tasks]
    assert len(claimed) == len(set(claimed)) == task_count
    assert set(claimed) == expected
    assert all(pg_queue.get(task)["attempts"] == 1 for task in claimed)


def test_postgres_expiry_rejects_stale_owner_and_persists_recovery(pg_queue):
    task_id = pg_queue.enqueue("job", "media", {}, max_attempts=2)
    assert pg_queue.claim("old", set()) is not None
    with pg_queue.db.session() as session:
        session.execute(update(TaskRow).where(TaskRow.id == task_id).values(lease_until=time.time() - 1))
    replacement = pg_queue.claim("new", set())
    assert replacement is not None and replacement.attempts == 2
    with pytest.raises(LeaseLost):
        pg_queue.complete(task_id, "old", {"stale": True})
    with pytest.raises(LeaseLost):
        pg_queue.renew(task_id, "old")
    pg_queue.complete(task_id, "new", {"recovered": True})
    assert pg_queue.output(pg_queue.get(task_id)) == {"recovered": True}


def test_postgres_reaper_cannot_overwrite_concurrent_renewal(pg_queue, monkeypatch):
    from course2md.core import queue as queue_module

    task_id = pg_queue.enqueue("job", "media", {})
    pg_queue.claim("worker", set())
    lease_until = time.time() + 20
    with pg_queue.db.session() as session:
        session.execute(update(TaskRow).where(TaskRow.id == task_id).values(lease_until=lease_until))
    observed, resumed = threading.Event(), threading.Event()

    def pause_reaper(_conn, _cursor, statement, _params, _context, _many):
        if threading.current_thread().name.startswith("reaper") and statement.lstrip().upper().startswith(
            "UPDATE TASKS"
        ):
            observed.set()
            assert resumed.wait(10)

    # 回收器时钟先看到过期；在其读出旧行后，由另一个连接续约。
    original_now = queue_module._now

    def now():
        return lease_until + 1 if threading.current_thread().name.startswith("reaper") else original_now()

    monkeypatch.setattr(queue_module, "_now", now)
    event.listen(pg_queue.db.engine, "before_cursor_execute", pause_reaper)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="reaper") as pool:
            future = pool.submit(pg_queue.reap)
            try:
                assert observed.wait(10)
                pg_queue.renew(task_id, "worker", lease_seconds=120)
            finally:
                resumed.set()
            assert future.result(timeout=10) == 0
        assert pg_queue.get(task_id)["status"] == "leased"
        pg_queue.complete(task_id, "worker", {"ok": True})
    finally:
        event.remove(pg_queue.db.engine, "before_cursor_execute", pause_reaper)


def test_postgres_cancelled_completion_does_not_create_artifact(pg_queue):
    task_id = pg_queue.enqueue("job", "organize", {})
    pg_queue.claim("worker", set())
    pg_queue.cancel(task_id)
    with pytest.raises(LeaseLost):
        pg_queue.complete(task_id, "worker", {"cancelled": True})
    with pg_queue.db.session() as session:
        assert list(session.scalars(select(ArtifactRow))) == []
    pg_queue.release(task_id, "worker")
    assert pg_queue.get(task_id)["status"] == "cancelled"

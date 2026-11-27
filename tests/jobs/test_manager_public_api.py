"""JobManager 对上层暴露的公开接口：加锁、共享对象、本机来源。"""

import threading

from course2md.jobs.job import Job
from course2md.jobs.manager import JobManager


def test_shared_creates_once_per_key(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    try:
        calls: list[str] = []

        def factory() -> object:
            calls.append("made")
            return object()

        first = manager.shared("demo", factory)
        assert manager.shared("demo", factory) is first
        assert manager.shared("other", object) is not first
        assert calls == ["made"]
    finally:
        manager.close()


def test_shared_is_thread_safe(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    try:
        created: list[object] = []
        results: list[object] = []

        def factory() -> object:
            created.append(object())
            return created[-1]

        threads = [threading.Thread(target=lambda: results.append(manager.shared("k", factory))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(created) == 1
        assert all(item is created[0] for item in results)
    finally:
        manager.close()


def test_locks_are_reentrant_and_ordered(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    job = Job("a", "x", "x", tmp_path / "out")
    try:
        # 约定的锁顺序：管理器锁在外，任务锁在内，且都可重入
        with manager.locked(), manager.locked(), job.locked(), job.locked():
            pass
    finally:
        manager.close()


def test_local_origin_urls_include_loopback_aliases(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    try:
        assert manager.hub_links.local_origin_urls() == set()
        manager.hub_links.set_local_origins({"http://127.0.0.1:8765"})
        assert manager.hub_links.local_origin_urls() >= {
            "http://127.0.0.1:8765",
            "http://localhost:8765",
        }
    finally:
        manager.close()

"""网状计算服务：多 Hub 互联、.env 自动连入、共享执行锁与权限边界。"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from course2md.core.accounts import Accounts
from course2md.hub.app import create_app
from course2md.jobs import manager as server
from course2md.worker.client import AuthError, HttpTransport, RemoteWorker
from course2md.worker.link import HubLinkManager, normalize_hub_url, token_hint
from live_server import LiveServer


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_normalize_and_token_hint():
    assert normalize_hub_url(" https://course.example.com/ ") == "https://course.example.com"
    with pytest.raises(ValueError, match="http:// 或 https://"):
        normalize_hub_url("ftp://example.com")
    assert token_hint("") == ""
    assert token_hint("short") == "****"
    assert token_hint("c2m_1234567890abcd") == "c2m_…abcd"


def test_env_auto_connect_and_multi_hub_mesh(tmp_path, monkeypatch):
    """一个节点可同时连入两个外部 Hub：一个从 .env 自动连入，一个经接口手动添加。"""
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", list)
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", list)

    hub_a_mgr = server.JobManager(tmp_path / "hub-a" / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    hub_b_mgr = server.JobManager(tmp_path / "hub-b" / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    _, token_a = hub_a_mgr.queue.create_worker_token("mesh-node-a")
    _, token_b = hub_b_mgr.queue.create_worker_token("mesh-node-b")

    node_mgr = server.JobManager(tmp_path / "node" / ".course2md" / "workspace.sqlite3", builtin_worker=True)
    node_app = create_app(node_mgr)

    hub_a = LiveServer(create_app(hub_a_mgr))
    hub_b = LiveServer(create_app(hub_b_mgr))
    hub_a_url = f"http://127.0.0.1:{hub_a.port}"
    hub_b_url = f"http://127.0.0.1:{hub_b.port}"
    try:
        monkeypatch.setenv("COURSE2MD_HUB_URL", hub_a_url)
        monkeypatch.setenv("COURSE2MD_WORKER_TOKEN", token_a)
        monkeypatch.setenv("COURSE2MD_WORKER_NAME", "LAPTOP-MESH")

        started = node_mgr.start_hub_links(local_origins={"http://127.0.0.1:8765"})
        assert len(started) == 1

        # .env 中的 Hub A 应自动上线
        assert _wait_until(
            lambda: any(
                item["status"] == "online" and item["source"] == "env" for item in node_mgr.hub_links.list_links()
            )
        )
        assert _wait_until(lambda: any(w["online"] and w["name"] == "LAPTOP-MESH" for w in hub_a_mgr.queue.workers()))

        with TestClient(node_app, base_url="http://127.0.0.1:8765") as client:
            # GET 接口绝不明文返回 token
            listing = client.get("/api/v1/hub-links").json()
            assert listing["can_manage"] is True
            assert len(listing["links"]) == 1
            assert "token" not in listing["links"][0]
            assert listing["links"][0]["has_token"] is True
            assert listing["links"][0]["token_hint"] == token_hint(token_a)

            # 阻止自连和重复添加
            assert (
                client.post(
                    "/api/v1/hub-links", json={"hub_url": "http://localhost:8765", "token": "c2m_x"}
                ).status_code
                == 400
            )
            assert client.post("/api/v1/hub-links", json={"hub_url": hub_a_url, "token": token_a}).status_code == 400

            # 手动添加第二个 Hub B，形成网状多连
            added = client.post(
                "/api/v1/hub-links",
                json={"hub_url": hub_b_url, "token": token_b, "name": "LAPTOP-B", "without": ["asr"]},
            )
            assert added.status_code == 200, added.text
            link_b_id = added.json()["link"]["id"]

            assert _wait_until(
                lambda: sum(1 for item in node_mgr.hub_links.list_links() if item["status"] == "online") == 2
            )
            workers_b = hub_b_mgr.queue.workers()
            assert any(w["online"] and w["name"] == "LAPTOP-B" and "asr" not in w["capabilities"] for w in workers_b)

            # 断开 Hub A 后，Hub A 立即看到离线，且重启不再被同一个 .env 强行覆盖
            link_a_id = listing["links"][0]["id"]
            disc = client.post(f"/api/v1/hub-links/{link_a_id}/disconnect", json={})
            assert disc.status_code == 200
            assert disc.json()["link"]["status"] == "disconnected"
            assert disc.json()["link"]["auto_connect"] is False
            assert _wait_until(lambda: all(not w["online"] for w in hub_a_mgr.queue.workers()))

            # 同样的 .env 不会覆盖用户显式断开的意愿
            assert node_mgr.hub_links.sync_env() == []
            assert node_mgr.hub_links.get_link(link_a_id)["auto_connect"] is False

            # .env 更换了新令牌后应自动同步并恢复 auto_connect
            _, token_a_new = hub_a_mgr.queue.create_worker_token("mesh-node-a-v2")
            monkeypatch.setenv("COURSE2MD_WORKER_TOKEN", token_a_new)
            assert node_mgr.hub_links.sync_env() == [link_a_id]
            assert node_mgr.hub_links.get_link(link_a_id)["auto_connect"] is True

            # 删除 Hub B 连接
            assert client.delete(f"/api/v1/hub-links/{link_b_id}").status_code == 200
            assert len(node_mgr.hub_links.list_links()) == 1
    finally:
        node_mgr.close()
        hub_a.stop()
        hub_b.stop()
        hub_a_mgr.close()
        hub_b_mgr.close()


def test_auth_failure_stops_retrying(tmp_path):
    """远程 Hub 返回 401 时状态变为 auth_failed 且停止重试。"""
    calls = {"register": 0}

    class FailingTransport(HttpTransport):
        def register_worker(self, worker_id, name, capabilities, kind="remote", version=""):
            calls["register"] += 1
            raise AuthError("节点令牌无效或已停用")

    def factory(cfg, caps, work_dir, resources, exec_lock, on_start, on_end):
        return RemoteWorker(
            FailingTransport(cfg.hub_url, cfg.token),
            caps,
            work_dir,
            name=cfg.name,
            resources=resources,
            exec_lock=exec_lock,
            on_task_start=on_start,
            on_task_end=on_end,
        )

    mgr = HubLinkManager(tmp_path / "hub-links.json", worker_factory=factory)
    try:
        link = mgr.add_link("https://hub.example.com", "c2m_badtoken123456")
        assert _wait_until(lambda: mgr.get_link(link["id"])["status"] == "auth_failed")
        time.sleep(0.2)
        assert calls["register"] == 1
        assert "令牌无效" in mgr.get_link(link["id"])["last_error"]
    finally:
        mgr.close()


def test_shared_exec_lock_prevents_claiming_while_busy(tmp_path):
    """共享执行锁被占用时，另一条节点连接不抢领任务，避免多路并发爆显存。"""
    lock = threading.Lock()
    shared_resources: dict = {"whisper": object()}
    claimed = {"count": 0}

    class CountingTransport:
        def register_worker(self, *args, **kwargs): ...
        def touch_worker(self, *args, **kwargs): ...
        def leave(self, *args, **kwargs): ...
        def claim(self, *args, **kwargs):
            claimed["count"] += 1
            return None

        def renew(self, *args, **kwargs):
            return False

        def release(self, *args, **kwargs): ...
        def progress(self, *args, **kwargs): ...
        def add_event(self, *args, **kwargs): ...
        def complete(self, *args, **kwargs):
            return ""

        def fail(self, *args, **kwargs): ...

    from course2md.worker.runner import WorkerRunner

    runner = WorkerRunner(
        CountingTransport(),
        {"asr"},
        resources=shared_resources,
        exec_lock=lock,
    )
    assert runner.resources is shared_resources
    with lock:
        assert runner.run_once() is False
        assert claimed["count"] == 0
    assert runner.run_once() is False
    assert claimed["count"] == 1


def test_server_mode_restricts_hub_links_to_admins(tmp_path, monkeypatch):
    """服务器模式下普通用户看不到也不能管理对外 Hub 连接；管理员可以管理但不能指定任意磁盘路径。"""
    monkeypatch.setenv("COURSE2MD_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", list)
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", list)

    manager = server.JobManager(tmp_path / "data" / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    manager.mode = "server"
    accounts = Accounts(manager.store.db, registration="open")
    app = create_app(manager, accounts=accounts)

    try:
        setup_token = accounts.begin_setup()
        admin = TestClient(app, base_url="https://hub.example.com")
        assert (
            admin.post(
                "/api/v1/auth/setup",
                json={"username": "admin", "password": "password-123", "setup_token": setup_token},
            ).status_code
            == 200
        )

        regular = TestClient(app, base_url="https://hub.example.com")
        assert (
            regular.post(
                "/api/v1/auth/register",
                json={"username": "alice", "password": "password-123"},
            ).status_code
            == 200
        )

        # 普通用户只看到 can_manage=False，且所有写操作被 403 拦截
        assert regular.get("/api/v1/hub-links").json()["can_manage"] is False
        assert (
            regular.post(
                "/api/v1/hub-links",
                json={"hub_url": "https://peer.example.com", "token": "c2m_1234567890", "connect_now": False},
            ).status_code
            == 403
        )

        # 管理员可添加，但自定义的 work_dir 在服务器模式下会被忽略
        res = admin.post(
            "/api/v1/hub-links",
            json={
                "hub_url": "https://peer.example.com",
                "token": "c2m_1234567890",
                "work_dir": str(tmp_path / "escape-dir"),
                "connect_now": False,
            },
        )
        assert res.status_code == 200, res.text
        link = res.json()["link"]
        assert "escape-dir" not in link["work_dir"]
        assert admin.delete(f"/api/v1/hub-links/{link['id']}").status_code == 200
    finally:
        manager.close()

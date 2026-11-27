"""服务器形态测试共用的夹具与辅助函数：带账号的 Hub、首个管理员注册、提交任务。

放在公共目录而不是某个测试文件里，这样各测试文件可以单独运行，不依赖其它测试模块的导入顺序。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from course2md.core.accounts import Accounts
from course2md.hub import auth
from course2md.hub.app import create_app
from course2md.jobs import manager as server
from resolver_patch import patch_get_resolver

SECRET_SOURCE = "https://classroom.example/play?token=very-secret"


@pytest.fixture
def hub(tmp_path, monkeypatch):

    monkeypatch.setenv("COURSE2MD_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", list)
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", list)
    patch_get_resolver(
        monkeypatch,
        lambda source: SimpleNamespace(
            name="local" if source.endswith(".mp4") else "fakeclass", list_sessions=lambda source: []
        ),
    )
    monkeypatch.setattr(auth, "_failures", auth.defaultdict(auth.deque))
    manager = server.JobManager(tmp_path / "data" / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    accounts = Accounts(manager.store.db, registration="closed")
    app = create_app(manager, accounts=accounts)

    def client():
        return TestClient(app, base_url="https://course.example.com")

    yield SimpleNamespace(manager=manager, accounts=accounts, client=client, data=tmp_path / "data")
    manager.close()


def register_first_admin(client, username, password="password-123"):
    """创建首个管理员：与真实部署一样，凭启动时生成的初始化令牌。"""
    token = client.app.state.accounts.begin_setup()
    response = client.post(
        "/api/v1/auth/setup", json={"username": username, "password": password, "setup_token": token}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def job_request(client, **extra):
    return client.post("/api/v1/jobs", json={"source": SECRET_SOURCE, "agent": "none", **extra})

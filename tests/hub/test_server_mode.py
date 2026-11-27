"""服务器形态：账号、按用户隔离、权限限制与凭证加密。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from course2md.core.accounts import Accounts, hash_password, verify_password
from course2md.core.models import CourseRow, JobRow, TaskRow
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


def _register(client, username, password="password-123"):
    """创建首个管理员：与真实部署一样，凭启动时生成的初始化令牌。"""
    token = client.app.state.accounts.begin_setup()
    response = client.post(
        "/api/v1/auth/setup", json={"username": username, "password": password, "setup_token": token}
    )
    assert response.status_code == 200, response.text
    return response.json()["user"]


def _job(client, **extra):
    return client.post("/api/v1/jobs", json={"source": SECRET_SOURCE, "agent": "none", **extra})


def test_password_hashes_are_salted():
    first, second = hash_password("口令-123456"), hash_password("口令-123456")
    assert first != second and verify_password("口令-123456", first) and not verify_password("wrong", first)


def test_first_admin_requires_setup_token_from_startup(hub):
    anonymous = hub.client()
    body = {"username": "teacher", "password": "password-123"}
    token = hub.accounts.begin_setup()
    assert token and token.startswith("c2s_") and hub.accounts.begin_setup() == token  # 重复调用不换令牌
    # 没有令牌的人既不能抢注，也不能通过自助注册接口绕过
    assert anonymous.post("/api/v1/auth/register", json=body).status_code == 400
    assert (
        "初始化令牌" in anonymous.post("/api/v1/auth/setup", json={**body, "setup_token": "c2s_wrong"}).json()["error"]
    )
    assert anonymous.post("/api/v1/auth/setup", json=body).status_code == 400
    assert hub.accounts.count() == 0
    created = anonymous.post("/api/v1/auth/setup", json={**body, "setup_token": token})
    assert created.status_code == 200 and created.json()["user"]["role"] == "admin"
    assert anonymous.get("/api/v1/auth/me").json()["user"]["username"] == "teacher"
    # 令牌一次有效，创建后作废；再次使用不能添加管理员
    assert hub.accounts.begin_setup() is None
    again = hub.client().post(
        "/api/v1/auth/setup", json={"username": "intruder", "password": "password-123", "setup_token": token}
    )
    assert again.status_code == 400 and "已完成初始化" in again.json()["error"]


def test_setup_attempts_are_throttled(hub):
    hub.accounts.begin_setup()
    attacker = hub.client()
    body = {"username": "teacher", "password": "password-123", "setup_token": "c2s_guess"}
    codes = [attacker.post("/api/v1/auth/setup", json=body).status_code for _ in range(12)]
    assert codes[:10] == [400] * 10 and codes[-1] == 429
    assert hub.accounts.count() == 0


def test_admin_manages_roles_and_resets_passwords(hub):
    admin = hub.client()
    _register(admin, "teacher")
    alice_id = admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"}).json()["user"]["id"]
    teacher_id = admin.get("/api/v1/auth/me").json()["user"]["id"]
    alice = hub.client()
    assert alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"}).status_code == 200
    token = alice.post("/api/v1/auth/tokens", json={}).json()["token"]

    assert admin.patch(f"/api/v1/users/{alice_id}", json={}).status_code == 400
    assert admin.patch(f"/api/v1/users/{alice_id}", json={"role": "root"}).status_code == 400
    assert admin.patch(f"/api/v1/users/{teacher_id}", json={"role": "user"}).status_code == 400  # 不能降级自己
    assert admin.patch(f"/api/v1/users/{teacher_id}", json={"password": "another-pass-1"}).status_code == 400
    assert admin.patch(f"/api/v1/users/{alice_id}", json={"password": "short"}).status_code == 400

    assert admin.patch(f"/api/v1/users/{alice_id}", json={"role": "admin"}).status_code == 200
    assert alice.get("/api/v1/users").status_code == 200
    assert admin.patch(f"/api/v1/users/{alice_id}", json={"password": "new-password-1"}).status_code == 200
    # 重置口令会注销该用户的会话与 API 令牌，旧口令不能再登录
    assert alice.get("/api/v1/jobs").status_code == 401
    assert hub.client().get("/api/v1/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    login = hub.client().post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    assert login.status_code == 400
    login = hub.client().post("/api/v1/auth/login", json={"username": "alice", "password": "new-password-1"})
    assert login.status_code == 200


def test_first_user_becomes_admin_and_registration_is_closed(hub):
    anonymous = hub.client()
    assert anonymous.get("/api/v1/auth/status").json() == {
        "mode": "server",
        "registration": "closed",
        "has_users": False,
        "needs_setup": True,
    }
    assert anonymous.get("/api/v1/jobs").status_code == 401
    admin = hub.client()
    assert _register(admin, "teacher")["role"] == "admin"
    assert admin.get("/api/v1/auth/me").json()["user"]["username"] == "teacher"
    response = hub.client().post("/api/v1/auth/register", json={"username": "student", "password": "password-123"})
    assert response.status_code == 400 and "未开放注册" in response.json()["error"]
    created = admin.post("/api/v1/users", json={"username": "student", "password": "password-123"})
    assert created.status_code == 200 and created.json()["user"]["role"] == "user"
    student = hub.client()
    login = student.post("/api/v1/auth/login", json={"username": "student", "password": "password-123"})
    assert login.status_code == 200 and login.json()["token"].startswith("c2u_")
    assert student.get("/api/v1/users").status_code == 403
    student.post("/api/v1/auth/logout", json={})
    assert student.get("/api/v1/jobs").status_code == 401


def test_bearer_tokens_and_cookies_are_secure(hub):
    client = hub.client()
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "teacher", "password": "password-123", "setup_token": hub.accounts.begin_setup()},
    )
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    token = client.post("/api/v1/auth/tokens", json={"name": "脚本"}).json()["token"]
    script = hub.client()
    assert script.get("/api/v1/jobs", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert script.get("/api/v1/jobs", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_users_only_see_their_own_courses_and_jobs(hub):
    admin = hub.client()
    _register(admin, "teacher")
    admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"})
    admin.post("/api/v1/users", json={"username": "bob", "password": "password-123"})
    alice, bob = hub.client(), hub.client()
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "password-123"})

    response = _job(alice, out_dir="C:/Windows")
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    alice_id = alice.get("/api/v1/auth/me").json()["user"]["id"]
    assert job["out_dir"].startswith(str(hub.data / "users" / alice_id))  # 忽略请求中的路径

    assert [item["id"] for item in alice.get("/api/v1/jobs").json()["jobs"]] == [job["id"]]
    assert bob.get("/api/v1/jobs").json()["jobs"] == []
    assert bob.get("/api/v1/courses").json()["courses"] == []
    for path in (
        f"/api/v1/jobs/{job['id']}",
        f"/api/v1/jobs/{job['id']}/tasks",
        f"/api/v1/jobs/{job['id']}/file?rel=job.json",
        f"/api/v1/courses/{job['course_id']}",
    ):
        assert bob.get(path).status_code == 404, path
    assert bob.post(f"/api/v1/jobs/{job['id']}/cancel", json={}).status_code == 404
    assert bob.post(f"/api/v1/jobs/{job['id']}/messages", json={"text": "hi"}).status_code == 404
    response = _job(bob, course_id=job["course_id"])
    assert response.status_code == 404


def test_server_restrictions_for_regular_users(hub):
    admin = hub.client()
    _register(admin, "teacher")
    admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"})
    alice = hub.client()
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    response = alice.post("/api/v1/jobs", json={"source": "D:/secret/lecture.mp4"})
    assert response.status_code == 403 and "本地文件" in response.json()["error"]
    assert _job(alice, agent="powershell -c whoami").status_code == 403
    assert _job(alice, llm_base="https://attacker.example/v1").status_code == 403
    config = alice.get("/api/v1/config").json()
    assert config["server"] and not config.get("default_out") and "model_path" not in config


def test_sources_are_encrypted_at_rest(hub):
    client = hub.client()
    _register(client, "teacher")
    job = _job(client).json()["job"]
    with hub.manager.store.db.session() as session:
        stored = [row.data for row in session.scalars(select(CourseRow))] + [
            row.data for row in session.scalars(select(JobRow))
        ]
        assert stored and all(SECRET_SOURCE not in data for data in stored)
        assert any("enc:v1:" in data for data in stored)
    assert hub.manager.store.load_jobs()[0]["source"] == SECRET_SOURCE
    assert client.get(f"/api/v1/jobs/{job['id']}").json()["source"].startswith("https://classroom.example")


def test_personal_nodes_only_claim_their_owners_tasks(hub):
    queue = hub.manager.queue
    queue.enqueue("job-a", "media", {"options": {"source": SECRET_SOURCE}}, owner_id="alice")
    queue.enqueue("job-b", "media", {"options": {"source": SECRET_SOURCE}}, owner_id="bob")
    with queue.db.session() as session:
        payloads = [json.dumps(row.payload) for row in session.scalars(select(TaskRow))]
    assert all(SECRET_SOURCE not in payload for payload in payloads)
    bob_node, _ = queue.create_worker_token("bob 的电脑", owner_id="bob")
    shared, _ = queue.create_worker_token("实验室服务器")
    claimed = queue.claim(bob_node, set())
    assert claimed.job_id == "job-b" and claimed.payload["options"]["source"] == SECRET_SOURCE
    assert queue.claim(bob_node, set()) is None
    assert queue.claim(shared, set()).job_id == "job-a"


@pytest.mark.parametrize("permanent", [False, True])
def test_node_tokens_are_scoped_to_users(hub, permanent):
    admin = hub.client()
    _register(admin, "teacher")
    admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"})
    alice = hub.client()
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    personal = alice.post("/api/v1/workers", json={"name": "我的电脑"}).json()
    lab = admin.post("/api/v1/workers", json={"name": "实验室"}).json()
    assert personal["shared"] is False and lab["shared"] is True
    alice_sees = {item["id"] for item in alice.get("/api/v1/workers").json()["workers"]}
    assert {personal["worker_id"], lab["worker_id"]} <= alice_sees
    params = {"permanent": permanent}
    assert alice.delete(f"/api/v1/workers/{lab['worker_id']}", params=params).status_code == 404
    assert alice.delete(f"/api/v1/workers/{personal['worker_id']}", params=params).status_code == 200
    workers = {item["id"]: item for item in alice.get("/api/v1/workers").json()["workers"]}
    if permanent:
        assert personal["worker_id"] not in workers
    else:
        assert workers[personal["worker_id"]]["disabled"] is True


@pytest.mark.parametrize("disabled", [False, True])
def test_worker_deletion_removes_record_and_revokes_token(hub, disabled):
    admin = hub.client()
    _register(admin, "teacher")
    worker = admin.post("/api/v1/workers", json={"name": "旧电脑"}).json()
    path = f"/api/v1/workers/{worker['worker_id']}"
    if disabled:
        assert admin.delete(path).status_code == 200
        assert admin.get("/api/v1/workers").json()["workers"][0]["disabled"] is True
    response = admin.delete(path, params={"permanent": True})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert admin.get("/api/v1/workers").json()["workers"] == []
    assert admin.delete(path, params={"permanent": True}).status_code == 404
    headers = {"Authorization": f"Bearer {worker['token']}"}
    assert admin.post("/api/v1/worker/heartbeat", headers=headers).status_code == 401
    assert admin.post("/api/v1/worker/register", headers=headers, json={"name": "旧电脑"}).status_code == 401
    assert admin.get("/api/v1/workers").json()["workers"] == []


@pytest.mark.parametrize("leased", [False, True])
def test_worker_deletion_protects_unfinished_tasks(hub, leased):
    admin = hub.client()
    _register(admin, "teacher")
    worker = admin.post("/api/v1/workers", json={"name": "工作电脑"}).json()
    worker_id = worker["worker_id"]
    queue = hub.manager.queue
    task_id = queue.enqueue("job", "organize", {}, affinity=None if leased else worker_id)
    if leased:
        assert queue.claim(worker_id, set()).id == task_id
    path = f"/api/v1/workers/{worker_id}"
    response = admin.delete(path, params={"permanent": True})
    assert response.status_code == 400 and "未完成任务" in response.json()["error"]
    assert queue.authenticate_worker(worker["token"])["id"] == worker_id
    assert queue.get(task_id)["status"] == ("leased" if leased else "pending")
    if leased:
        queue.fail(task_id, worker_id, "任务已取消", cancelled=True)
    else:
        queue.cancel(task_id)
    assert admin.delete(path, params={"permanent": True}).status_code == 200


def test_only_admin_can_delete_another_users_node_and_builtin_is_protected(hub):
    admin = hub.client()
    _register(admin, "teacher")
    admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"})
    admin.post("/api/v1/users", json={"username": "bob", "password": "password-123"})
    alice, bob = hub.client(), hub.client()
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    bob.post("/api/v1/auth/login", json={"username": "bob", "password": "password-123"})
    worker = alice.post("/api/v1/workers", json={"name": "Alice 的电脑"}).json()
    path = f"/api/v1/workers/{worker['worker_id']}"
    assert hub.client().delete(path, params={"permanent": True}).status_code == 401
    assert bob.delete(path, params={"permanent": True}).status_code == 404
    assert hub.manager.queue.authenticate_worker(worker["token"]) is not None
    assert admin.delete(path, params={"permanent": True}).status_code == 200
    hub.manager.queue.register_worker("builtin", "内置节点", {"hub"})
    assert admin.delete("/api/v1/workers/builtin", params={"permanent": True}).status_code == 404
    assert admin.get("/api/v1/workers").json()["workers"][0]["id"] == "builtin"


def test_disabled_users_lose_sessions_and_failed_logins_are_throttled(hub):
    admin = hub.client()
    _register(admin, "teacher")
    alice_id = admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"}).json()["user"]["id"]
    alice = hub.client()
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    assert admin.patch(f"/api/v1/users/{alice_id}", json={"disabled": True}).status_code == 200
    assert alice.get("/api/v1/jobs").status_code == 401
    attacker = hub.client()
    codes = [
        attacker.post("/api/v1/auth/login", json={"username": "teacher", "password": "guess"}).status_code
        for _ in range(12)
    ]
    assert codes[:10] == [400] * 10 and codes[-1] == 429

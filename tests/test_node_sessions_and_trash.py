"""服务器形态：由节点解析课次、课程回收站与到期清理。"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from course2md.core.models import TaskRow
from course2md.hub import service
from course2md.jobs import manager as server
from course2md.resolvers import ResolverError
from course2md.resolvers.base import SessionInfo
from course2md.stages.artifacts import SessionList, load_artifact
from course2md.worker import WorkerRunner
from test_server_mode import SECRET_SOURCE, hub  # noqa: F401  复用服务器形态的夹具
from test_server_mode import _job as job_request
from test_server_mode import _register as register_first_admin


@pytest.fixture
def node(hub, monkeypatch):  # noqa: F811
    """在 Hub 的任务队列上运行一个只具备 resolver:fakeclass 能力的节点；Hub 自己的解析器返回空列表。"""
    runners: list[WorkerRunner] = []
    calls: list[str] = []
    state = SimpleNamespace(error=None)

    def list_sessions(source):
        calls.append(source)
        if state.error:
            raise state.error
        return [
            SessionInfo("s1", "2026-03-02", "第一讲", views=2, course="高数"),
            SessionInfo("s2", "2026-03-09", "第二讲", views=1, course="高数"),
        ]

    fake = SimpleNamespace(name="fakeclass", list_sessions=list_sessions)
    monkeypatch.setattr("course2md.stages.sessions.get_resolver", lambda source: fake)

    def start(worker_id="node-a", owner=None, capabilities=("resolver:fakeclass",)):
        if owner:
            worker_id, _token = hub.manager.queue.create_worker_token("个人节点", owner_id=owner)
        runner = WorkerRunner(
            hub.manager.queue, set(capabilities), worker_id=worker_id, kind="remote", poll_interval=0.05
        ).start()
        runners.append(runner)
        return runner

    yield SimpleNamespace(start=start, calls=calls, state=state)
    for runner in runners:
        runner.stop()


def test_session_list_artifact_round_trips():
    original = SessionList([SessionInfo("s1", "2026-03-02", "第一讲", views=2, course="高数", meta={"begin": "08:00"})])
    restored = load_artifact(original.to_dict())
    assert isinstance(restored, SessionList) and restored.sessions == original.sessions


def test_resolve_runs_on_a_node_instead_of_the_hub(hub, node):  # noqa: F811
    admin = hub.client()
    register_first_admin(admin, "teacher")
    response = admin.post("/api/v1/resolve", json={"source": SECRET_SOURCE})
    assert response.status_code == 400 and "没有在线的计算节点" in response.json()["error"]
    assert "resolver:fakeclass" in response.json()["error"]

    node.start()
    response = admin.post("/api/v1/resolve", json={"source": SECRET_SOURCE})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["sessions"]] == ["s1", "s2"]  # Hub 自己的解析器返回的是空列表
    assert node.calls == [SECRET_SOURCE]
    with hub.manager.store.db.session() as session:  # 解析任务（载荷里带来源凭证）用完即删
        assert not session.scalars(select(TaskRow).where(TaskRow.stage == "sessions")).all()


def test_worker_without_resolver_never_takes_session_listing(hub, node):  # noqa: F811
    """不具备对应 resolver 能力的节点不会领取课次解析任务。"""
    admin = hub.client()
    register_first_admin(admin, "teacher")
    node.start(worker_id="no-resolver", capabilities=("asr", "llm-api"))
    response = admin.post("/api/v1/resolve", json={"source": SECRET_SOURCE})
    assert response.status_code == 400 and "resolver:fakeclass" in response.json()["error"]
    assert node.calls == []


def test_capabilities_detect_llm_api_accurately_and_drop_node_pseudo_cap(monkeypatch):
    from course2md.worker import detect_capabilities

    for key in ("COURSE2MD_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "COURSE2MD_LLM_BASE"):
        monkeypatch.delenv(key, raising=False)
    caps_no_key = detect_capabilities(agents=False)
    assert "node" not in caps_no_key
    assert "hub" not in caps_no_key
    assert "llm-api" not in caps_no_key
    assert "hub" in detect_capabilities(hub=True, agents=False)

    monkeypatch.setenv("COURSE2MD_LLM_BASE", "http://127.0.0.1:11434/v1")
    assert "llm-api" in detect_capabilities(agents=False)

    monkeypatch.delenv("COURSE2MD_LLM_BASE", raising=False)
    monkeypatch.setenv("COURSE2MD_API_KEY", "sk-test")
    assert "llm-api" in detect_capabilities(agents=False)


def test_server_cli_defaults_to_pure_hub_and_supports_with_node():
    from course2md.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["server"]).with_node is False
    assert parser.parse_args(["server", "--with-node"]).with_node is True


def test_node_resolution_failures_are_reported_as_resolver_errors(hub, node):  # noqa: F811
    admin = hub.client()
    register_first_admin(admin, "teacher")
    node.start()
    node.state.error = ResolverError("登录已过期，请重新复制播放链接")
    response = admin.post("/api/v1/resolve", json={"source": SECRET_SOURCE})
    assert response.status_code == 400 and "登录已过期" in response.json()["error"]


def test_personal_nodes_only_resolve_for_their_owner(hub, node):  # noqa: F811
    admin = hub.client()
    register_first_admin(admin, "teacher")
    alice_id = admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"}).json()["user"]["id"]
    alice = hub.client()
    alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"})
    node.start(owner=alice_id)
    assert alice.post("/api/v1/resolve", json={"source": SECRET_SOURCE}).status_code == 200
    other = admin.post("/api/v1/resolve", json={"source": SECRET_SOURCE})
    assert other.status_code == 400 and "没有在线的计算节点" in other.json()["error"]


def test_submitting_uses_saved_sessions_without_asking_a_node_again(hub, node):  # noqa: F811
    admin = hub.client()
    register_first_admin(admin, "teacher")
    runner = node.start()
    course = admin.post("/api/v1/resolve", json={"source": SECRET_SOURCE}).json()
    runner.stop()  # 节点离线后仍能凭解析时保存的课次创建任务
    calls = len(node.calls)
    response = job_request(admin, course_id=course["course_id"], session_ids=["s2"])
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    assert job["session_id"] == "s2" and job["title"] == "第二讲"
    assert len(node.calls) == calls


# ---------------------------------------------------------------- 回收站
def _finished_course(hub, owner_id):  # noqa: F811
    root = hub.data / "users" / owner_id
    course = hub.manager.remember_course(SECRET_SOURCE, root, {"course": "高数", "sessions": []}, owner=owner_id)
    folder = root / "高数" / "2026-03-02"
    folder.mkdir(parents=True)
    (folder / "notes.md").write_text("笔记", encoding="utf-8")
    job = server.Job(
        id="job1",
        source=SECRET_SOURCE,
        title="第一讲",
        out_dir=folder,
        course="高数",
        course_id=course["id"],
        owner=owner_id,
        status="done",
    )
    hub.manager.jobs[job.id] = job
    hub.manager._save(job)
    return course, job


def test_deleted_courses_go_to_trash_and_can_be_restored(hub):  # noqa: F811
    admin = hub.client()
    owner = register_first_admin(admin, "teacher")["id"]
    course, job = _finished_course(hub, owner)
    other = hub.client()
    admin.post("/api/v1/users", json={"username": "bob", "password": "password-123"})
    other.post("/api/v1/auth/login", json={"username": "bob", "password": "password-123"})

    assert other.delete(f"/api/v1/courses/{course['id']}").status_code == 404  # 不能删除别人的课程
    assert admin.delete(f"/api/v1/courses/{course['id']}").status_code == 200
    assert admin.get("/api/v1/courses").json()["courses"] == []
    assert admin.get("/api/v1/jobs").json()["jobs"] == []
    assert admin.get(f"/api/v1/jobs/{job.id}").status_code == 404
    assert admin.get(f"/api/v1/courses/{course['id']}").status_code == 404

    trash = admin.get("/api/v1/trash").json()
    assert trash["retention_days"] == 30 and trash["removes_files"] is True
    assert [(item["kind"], item["id"], item["job_count"]) for item in trash["items"]] == [("course", course["id"], 1)]
    assert other.get("/api/v1/trash").json()["items"] == []
    assert other.post(f"/api/v1/trash/course/{course['id']}/restore", json={}).status_code == 404

    # 回收站状态随记录保存，重启后仍然在
    assert any(data.get("deleted_at") for data in hub.manager.store.load_jobs())
    assert any(data.get("deleted_at") for data in hub.manager.store.load_courses())

    assert admin.post(f"/api/v1/trash/course/{course['id']}/restore", json={}).status_code == 200
    assert [item["id"] for item in admin.get("/api/v1/jobs").json()["jobs"]] == [job.id]
    assert [item["id"] for item in admin.get("/api/v1/courses").json()["courses"]] == [course["id"]]
    assert admin.get("/api/v1/trash").json()["items"] == []


def test_deleting_a_single_job_lists_it_separately(hub):  # noqa: F811
    admin = hub.client()
    owner = register_first_admin(admin, "teacher")["id"]
    course, job = _finished_course(hub, owner)
    assert admin.delete(f"/api/v1/jobs/{job.id}").status_code == 200
    assert [item["id"] for item in admin.get("/api/v1/courses").json()["courses"]] == [course["id"]]
    assert admin.get("/api/v1/jobs").json()["jobs"] == []
    assert [(item["kind"], item["id"]) for item in admin.get("/api/v1/trash").json()["items"]] == [("job", job.id)]
    assert admin.post(f"/api/v1/trash/job/{job.id}/restore", json={}).status_code == 200
    assert [item["id"] for item in admin.get("/api/v1/jobs").json()["jobs"]] == [job.id]


def test_deleting_cancels_running_jobs_and_they_are_not_purged_while_active(hub):  # noqa: F811
    admin = hub.client()
    owner = register_first_admin(admin, "teacher")["id"]
    _course, job = _finished_course(hub, owner)
    job.status = "running"
    assert admin.delete(f"/api/v1/jobs/{job.id}").status_code == 200
    assert job.cancel.is_set()
    assert hub.manager.purge_deleted(now=time.time() + 40 * 86400) == 0 and job.id in hub.manager.jobs


def test_expired_trash_is_purged_with_its_files_on_the_server(hub):  # noqa: F811
    admin = hub.client()
    owner = register_first_admin(admin, "teacher")["id"]
    course, job = _finished_course(hub, owner)
    folder = job.out_dir
    now = time.time()
    admin.delete(f"/api/v1/courses/{course['id']}")
    assert hub.manager.purge_deleted(now=now + 29 * 86400) == 0 and folder.is_dir()
    assert hub.manager.purge_deleted(now=now + 31 * 86400) == 1
    assert not folder.exists() and not folder.parent.exists()  # 课程文件夹变空后一并清理
    assert (hub.data / "users" / owner).is_dir()
    assert hub.manager.store.load_jobs() == [] and hub.manager.store.load_courses() == []
    assert admin.get("/api/v1/trash").json()["items"] == []


def test_trash_can_be_emptied_immediately(hub):  # noqa: F811
    admin = hub.client()
    owner = register_first_admin(admin, "teacher")["id"]
    course, job = _finished_course(hub, owner)
    assert admin.delete(f"/api/v1/trash/course/{course['id']}").status_code == 404  # 未删除的不能直接清除
    admin.delete(f"/api/v1/courses/{course['id']}")
    assert admin.delete(f"/api/v1/trash/course/{course['id']}").status_code == 200
    assert not job.out_dir.exists() and hub.manager.store.load_jobs() == []


def test_local_app_purge_never_deletes_the_users_own_files(tmp_path):
    manager = server.JobManager(tmp_path / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    try:
        vault = tmp_path / "我的知识库" / "高数"
        vault.mkdir(parents=True)
        (vault / "notes.md").write_text("笔记", encoding="utf-8")
        course = manager.remember_course("https://example.com/c", tmp_path / "我的知识库", {"course": "高数"})
        job = server.Job(
            id="job1", source="x", title="第一讲", out_dir=vault, course_id=course["id"], owner="local", status="done"
        )
        manager.jobs[job.id] = job
        manager._save(job)
        service.delete_course(manager, course["id"])
        assert manager.purge_deleted(now=time.time() + 31 * 86400) == 1
        assert (vault / "notes.md").read_text(encoding="utf-8") == "笔记"  # 只清理记录
        assert manager.store.load_jobs() == [] and manager.store.load_courses() == []
    finally:
        manager.close()

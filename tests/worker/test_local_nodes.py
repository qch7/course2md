"""本机节点：在 Hub 进程内创建多个节点，各自声明能力，重启后保留。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_server_mode import _register as register_first_admin
from test_server_mode import hub  # noqa: F401  复用服务器形态的夹具

from course2md.hub.app import create_app
from course2md.jobs import manager as server


@pytest.fixture
def local_manager(tmp_path, monkeypatch):
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", list)
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", list)
    manager = server.JobManager(tmp_path / "out" / ".course2md" / "workspace.sqlite3")
    yield manager
    manager.close()


@pytest.fixture
def client(local_manager):
    with TestClient(create_app(local_manager), base_url="http://127.0.0.1:8765") as http:
        yield http


def _worker(client, worker_id):
    return next((item for item in client.get("/api/v1/workers").json()["workers"] if item["id"] == worker_id), None)


def test_local_app_lists_the_builtin_node_with_available_capabilities(client):
    data = client.get("/api/v1/local-nodes").json()
    assert data["can_manage"] is True
    builtin = next(item for item in data["nodes"] if item["id"] == "builtin")
    assert builtin["builtin"] and builtin["auto"] and builtin["online"] and builtin["enabled"]
    assert set(builtin["capabilities"]) == set(data["available_capabilities"])
    assert {"hub", "frame-selector:general"} <= set(builtin["capabilities"])


def test_create_node_with_a_capability_subset_registers_it_in_the_queue(client):
    response = client.post(
        "/api/v1/local-nodes", json={"name": "只做画面", "capabilities": ["frame-selector:general", "hub"]}
    )
    assert response.status_code == 200, response.text
    node = response.json()["node"]
    assert node["capabilities"] == ["frame-selector:general", "hub"] and not node["auto"] and node["online"]
    assert not node["builtin"] and node["id"].startswith("local-")
    registered = _worker(client, node["id"])
    assert registered["online"] and registered["kind"] == "builtin" and registered["name"] == "只做画面"
    assert registered["capabilities"] == ["frame-selector:general", "hub"]


def test_create_node_rejects_unknown_empty_and_duplicate_choices(client):
    bad = client.post("/api/v1/local-nodes", json={"name": "A", "capabilities": ["quantum"]})
    assert bad.status_code == 400 and "quantum" in bad.json()["error"]
    empty = client.post("/api/v1/local-nodes", json={"name": "A", "capabilities": []})
    assert empty.status_code == 400 and "至少选择一项能力" in empty.json()["error"]
    assert client.post("/api/v1/local-nodes", json={"name": "B", "auto": True}).status_code == 200
    dup = client.post("/api/v1/local-nodes", json={"name": "B", "auto": True})
    assert dup.status_code == 400 and "B" in dup.json()["error"]


def test_update_changes_capabilities_name_and_enabled_state(client):
    node_id = client.post("/api/v1/local-nodes", json={"name": "节点甲", "capabilities": ["hub"]}).json()["node"]["id"]

    updated = client.patch(f"/api/v1/local-nodes/{node_id}", json={"capabilities": ["frame-selector:general"]})
    assert updated.json()["node"]["capabilities"] == ["frame-selector:general"]
    assert _worker(client, node_id)["capabilities"] == ["frame-selector:general"]

    renamed = client.patch(f"/api/v1/local-nodes/{node_id}", json={"name": "节点乙"}).json()["node"]
    assert renamed["name"] == "节点乙" and renamed["online"]
    assert _worker(client, node_id)["name"] == "节点乙"

    disabled = client.patch(f"/api/v1/local-nodes/{node_id}", json={"enabled": False}).json()["node"]
    assert disabled["enabled"] is False and disabled["online"] is False
    assert _worker(client, node_id)["online"] is False

    auto = client.patch(f"/api/v1/local-nodes/{node_id}", json={"enabled": True, "auto": True}).json()["node"]
    assert auto["online"] and auto["auto"] and "hub" in auto["capabilities"]

    assert client.patch("/api/v1/local-nodes/missing", json={"name": "x"}).status_code == 404
    unsupported = client.patch(f"/api/v1/local-nodes/{node_id}", json={"auto": False, "capabilities": ["quantum"]})
    assert unsupported.status_code == 400


def test_builtin_node_can_be_edited_and_disabled_but_not_deleted(client, local_manager):
    assert local_manager.builtin_worker is not None
    disabled = client.patch("/api/v1/local-nodes/builtin", json={"enabled": False}).json()["node"]
    assert disabled["online"] is False and local_manager.builtin_worker is None
    refused = client.delete("/api/v1/local-nodes/builtin")
    assert refused.status_code == 400 and "不能删除" in refused.json()["error"]
    client.patch("/api/v1/local-nodes/builtin", json={"enabled": True})
    assert local_manager.builtin_worker is not None


def test_delete_removes_the_node_and_its_queue_record(client):
    node_id = client.post("/api/v1/local-nodes", json={"name": "临时", "auto": True}).json()["node"]["id"]
    assert _worker(client, node_id) is not None
    assert client.delete(f"/api/v1/local-nodes/{node_id}").json() == {"ok": True}
    assert _worker(client, node_id) is None
    assert all(item["id"] != node_id for item in client.get("/api/v1/local-nodes").json()["nodes"])
    assert client.delete(f"/api/v1/local-nodes/{node_id}").status_code == 404


def test_nodes_and_their_capabilities_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", list)
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", list)
    path = tmp_path / "out" / ".course2md" / "workspace.sqlite3"
    first = server.JobManager(path)
    try:
        assert first.local_nodes is not None
        created = first.local_nodes.add_node("只读画面", capabilities=["frame-selector:general"])
        first.local_nodes.update_node("builtin", enabled=False)
    finally:
        first.close()

    second = server.JobManager(path)
    try:
        assert second.local_nodes is not None
        nodes = {item["id"]: item for item in second.local_nodes.list_nodes()}
        assert nodes[created["id"]]["capabilities"] == ["frame-selector:general"] and nodes[created["id"]]["online"]
        assert nodes["builtin"]["enabled"] is False and nodes["builtin"]["online"] is False
    finally:
        second.close()


def test_limit_on_the_number_of_local_nodes(local_manager):
    nodes = local_manager.local_nodes
    assert nodes is not None
    from course2md.worker.local import MAX_LOCAL_NODES

    for index in range(MAX_LOCAL_NODES - 1):
        nodes.add_node(f"节点 {index}", auto=True, enabled=False)
    with pytest.raises(ValueError, match="最多"):
        nodes.add_node("多出来的", auto=True)


def test_pure_hub_has_no_local_nodes_and_cannot_create_them(hub):  # noqa: F811
    admin = hub.client()
    register_first_admin(admin, "teacher")
    data = admin.get("/api/v1/local-nodes").json()
    assert data == {"nodes": [], "can_manage": False, "available_capabilities": []}
    assert admin.post("/api/v1/local-nodes", json={"name": "x", "auto": True}).status_code == 404


def test_only_admins_manage_local_nodes_on_a_server(hub):  # noqa: F811
    admin = hub.client()
    register_first_admin(admin, "teacher")
    admin.post("/api/v1/users", json={"username": "alice", "password": "password-123"})
    alice = hub.client()
    assert alice.post("/api/v1/auth/login", json={"username": "alice", "password": "password-123"}).status_code == 200
    assert alice.get("/api/v1/local-nodes").json()["can_manage"] is False
    assert alice.post("/api/v1/local-nodes", json={"name": "x", "auto": True}).status_code == 403

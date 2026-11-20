"""网页插件管理：安装来源、包校验、权限、上传与真实隔离环境安装。"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import venv
import zipfile
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from course2md.core.accounts import User
from course2md.hub import plugins
from course2md.hub.app import create_app
from course2md.hub.service import Forbidden
from course2md.resolvers import ResolverInfo


def distribution(name="course2md-demo"):
    return SimpleNamespace(
        name=name,
        metadata={"Name": name},
        version="0.1.0",
        entry_points=[metadata.EntryPoint("demo", "course2md_demo:DemoResolver", "course2md.resolvers")],
    )


@pytest.fixture
def management(monkeypatch):
    manager = SimpleNamespace(mode="local", jobs={}, _lock=threading.RLock())
    installed = []
    calls = []
    staged = [distribution()]
    monkeypatch.setattr(plugins, "_restart_required", False)
    monkeypatch.setattr(plugins, "_operation_lock", threading.Lock())
    monkeypatch.setattr(
        plugins, "available", lambda: [ResolverInfo("local", "本地视频", "本地视频文件或目录", -100, "builtin")]
    )
    monkeypatch.setattr(plugins, "load_errors", list)
    monkeypatch.setattr(plugins, "_distributions", lambda path=None: staged if path is not None else installed)

    def run(arguments, cache_dir):
        calls.append(arguments)
        if "--constraint" in arguments:
            constraint = Path(arguments[arguments.index("--constraint") + 1])
            assert constraint.read_text(encoding="utf-8") == f"course2md=={metadata.version('course2md')}\n"
            installed[:] = staged
        elif arguments[0] == "uninstall":
            installed[:] = [dist for dist in installed if dist.metadata["Name"] != arguments[1]]

    monkeypatch.setattr(plugins, "_run_package", run)
    with TestClient(create_app(manager), base_url="http://127.0.0.1:8765") as client:
        yield SimpleNamespace(manager=manager, installed=installed, staged=staged, calls=calls, client=client)


@pytest.mark.parametrize("suffix", ["", ".git", "/tree/v1.2"])
def test_github_repository_is_installed_then_restart_is_required(management, suffix):
    response = management.client.post(
        "/api/v1/plugins", json={"source": f"https://github.com/example/course2md-demo{suffix}", "kind": "github"}
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["restart_required"] and data["can_manage"]
    assert {item["name"] for item in data["plugins"]} == {"course2md-demo", "local"}
    expected = "git+https://github.com/example/course2md-demo.git" + ("@v1.2" if "/tree/" in suffix else "")
    assert len(management.calls) == 2 and all(call[-1] == expected for call in management.calls)
    assert "--target" in management.calls[0] and "--no-deps" in management.calls[0]
    response = management.client.post("/api/v1/resolve", json={"source": "demo://lesson"})
    assert response.status_code == 400 and "重启" in response.json()["error"]
    response = management.client.post("/api/v1/jobs", json={"source": "demo://lesson"})
    assert response.status_code == 400 and "重启" in response.json()["error"]


@pytest.mark.parametrize(
    "source",
    [
        "",
        " ",
        "https://example.com/a/b",
        "https://github.com/a/b?token=x",
        "https://user:pass@github.com/a/b",
        "https://github.com/a/b/issues",
        "--help",
    ],
)
def test_invalid_github_sources_do_not_run_installer(management, source):
    response = management.client.post("/api/v1/plugins", json={"source": source, "kind": "github"})
    assert response.status_code == 400
    assert management.calls == []


def test_local_project_path_is_supported(management, tmp_path):
    project = tmp_path / "plugin with spaces"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='course2md-demo'\n", encoding="utf-8")
    response = management.client.post("/api/v1/plugins", json={"source": str(project), "kind": "local"})
    assert response.status_code == 200 and management.calls[-1][-1] == str(project.resolve())


@pytest.mark.parametrize("core", [False, True])
def test_non_plugin_and_core_packages_are_not_installed(management, core):
    management.staged[:] = [distribution("course2md")] if core else []
    response = management.client.post("/api/v1/plugins", json={"source": "https://github.com/a/b"})
    assert response.status_code == 400 and "入口点" in response.json()["error"]
    assert len(management.calls) == 1
    assert not plugins._restart_required


def test_uploaded_package_is_imported_and_temporary_file_is_cleaned(management, monkeypatch):
    seen = {}

    def install(manager, source, kind, user):
        seen["path"] = Path(source)
        assert seen["path"].read_bytes() == b"wheel-data"
        assert kind == "local" and manager is management.manager
        return plugins.list_plugins(manager, user)

    monkeypatch.setattr(plugins, "install_plugin", install)
    response = management.client.post(
        "/api/v1/plugins/import?filename=course2md_demo-0.1.0-py3-none-any.whl",
        content=b"wheel-data",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text
    assert not seen["path"].exists()


@pytest.mark.parametrize("filename", ["../bad.whl", "..\\bad.whl", "C:bad.whl", "plugin.py", "bad\x00.whl"])
def test_upload_rejects_invalid_filenames(management, filename):
    response = management.client.post(
        "/api/v1/plugins/import",
        params={"filename": filename},
        content=b"data",
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 400 and not management.calls


def test_upload_preserves_origin_and_size_guards(management):
    path = "/api/v1/plugins/import?filename=plugin.zip"
    assert (
        management.client.post(path, content=b"", headers={"Content-Type": "application/octet-stream"}).status_code
        == 400
    )
    response = management.client.post(
        path, content=b"x", headers={"Content-Type": "application/octet-stream", "Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    response = management.client.post(
        path,
        content=b"x",
        headers={"Content-Type": "application/octet-stream", "Content-Length": str(plugins.MAX_UPLOAD_BYTES + 1)},
    )
    assert response.status_code == 400
    assert (
        management.client.post(
            "/api/v1/jobs", content=b"x", headers={"Content-Type": "application/octet-stream"}
        ).status_code
        == 403
    )
    assert not management.calls


def test_plugin_deletion_preserves_builtin_and_other_packages(management):
    management.installed.append(distribution())
    assert management.client.delete("/api/v1/plugins/local").status_code == 404
    assert management.client.delete("/api/v1/plugins/course2md").status_code == 404
    assert management.client.delete("/api/v1/plugins/setuptools").status_code == 404
    response = management.client.delete("/api/v1/plugins/course2md-demo")
    assert response.status_code == 200 and response.json()["restart_required"]
    assert [item["name"] for item in response.json()["plugins"]] == ["local"]
    assert management.calls == [["uninstall", "course2md-demo"]]


def test_busy_tasks_and_concurrent_changes_are_rejected(management):
    management.manager.jobs["job"] = SimpleNamespace(status="running", pending_messages=0)
    response = management.client.post("/api/v1/plugins", json={"source": "https://github.com/a/b"})
    assert response.status_code == 400 and "课程仍在处理" in response.json()["error"]
    management.manager.jobs.clear()
    with plugins._operation_lock:
        response = management.client.post("/api/v1/plugins", json={"source": "https://github.com/a/b"})
    assert response.status_code == 400 and "正在进行" in response.json()["error"]
    assert not management.calls


def test_server_and_non_admin_management_is_read_only(management):
    management.manager.mode = "server"
    assert management.client.get("/api/v1/plugins").json()["can_manage"] is False
    assert management.client.post("/api/v1/plugins", json={"source": "https://github.com/a/b"}).status_code == 403
    assert management.client.delete("/api/v1/plugins/course2md-demo").status_code == 403
    management.manager.mode = "local"
    with pytest.raises(Forbidden):
        plugins.install_plugin(management.manager, "https://github.com/a/b", "github", User("alice", "Alice"))
    assert not management.calls


def test_package_runner_uses_argument_list_and_current_python(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(plugins.shutil, "which", lambda name: "uv-test")
    monkeypatch.setattr(
        plugins.subprocess,
        "run",
        lambda command, **options: seen.append((command, options)) or SimpleNamespace(returncode=0),
    )
    plugins._run_package(["uninstall", "course2md-demo"], tmp_path / "cache")
    command, options = seen[0]
    assert command == ["uv-test", "pip", "uninstall", "--python", sys.executable, "course2md-demo"]
    assert not options.get("shell") and options["env"]["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.integration
def test_real_wheel_install_load_and_delete_in_isolated_environment(tmp_path, monkeypatch):
    if not shutil.which("uv"):
        pytest.skip("真实隔离环境测试需要 uv")
    environment = tmp_path / "environment"
    venv.EnvBuilder(with_pip=False, symlinks=sys.platform != "win32").create(environment)
    python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    site = environment / (
        "Lib/site-packages"
        if sys.platform == "win32"
        else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    )
    wheel = tmp_path / "course2md_demo-0.1.0-py3-none-any.whl"
    files = {
        "course2md_demo/__init__.py": "class DemoResolver:\n    name = 'demo'\n    def matches(self, source):\n        return source.startswith('demo://')\n    def list_sessions(self, source):\n        return []\n    def resolve(self, source, session_id=None, views=None):\n        return []\n",
        "course2md_demo-0.1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: course2md-demo\nVersion: 0.1.0\n",
        "course2md_demo-0.1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: course2md-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "course2md_demo-0.1.0.dist-info/entry_points.txt": "[course2md.resolvers]\ndemo = course2md_demo:DemoResolver\n",
    }
    record = "course2md_demo-0.1.0.dist-info/RECORD"
    files[record] = "\n".join(f"{name},," for name in [*files, record]) + "\n"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    original = plugins._distributions
    monkeypatch.setattr(plugins, "_distributions", lambda path=None: original(path if path is not None else site))
    monkeypatch.setattr(plugins, "_restart_required", False)
    monkeypatch.setattr(plugins.sys, "executable", str(python))
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    manager = SimpleNamespace(mode="local", jobs={}, _lock=threading.RLock())
    result = plugins.install_plugin(manager, str(wheel), "local")
    assert any(item["name"] == "course2md-demo" for item in result["plugins"])
    loaded = subprocess.run(
        [
            str(python),
            "-c",
            "from importlib.metadata import entry_points; resolver = next(iter(entry_points(group='course2md.resolvers'))).load()(); assert resolver.matches('demo://lesson')",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert loaded.returncode == 0, loaded.stderr
    retained = site / "unrelated.txt"
    retained.write_text("keep", encoding="utf-8")
    result = plugins.delete_plugin(manager, "course2md-demo")
    assert not any(item["name"] == "course2md-demo" for item in result["plugins"])
    assert retained.read_text(encoding="utf-8") == "keep"

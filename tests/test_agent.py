"""Codex 命令发现、标准输入与笔记产物检查。"""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from course2md import agents as agent
from course2md.agents import codex, dsh


@pytest.mark.parametrize("hive", ["user", "machine"])
@pytest.mark.parametrize("view", [0x100, 0x200])
def test_dsh_discovers_custom_install_from_windows_registry(tmp_path, monkeypatch, hive, view):
    root = tmp_path / "custom apps" / "DSH"
    root.mkdir(parents=True)
    (root / "DeepSeek Harness.exe").touch()
    (root / "resources").mkdir()
    (root / "resources" / "app.asar").touch()
    records = [
        {"DisplayName": "Another application"},
        {"DisplayName": "DeepSeek Harness"},  # 残留记录缺少安装路径，继续查找
        {"DisplayName": "DeepSeek Harness", "InstallLocation": str(root)},
        {"DisplayName": "DeepSeek Harness", "InstallLocation": str(root)},
    ]

    def open_key(parent, name, reserved=0, access=0):
        if isinstance(parent, tuple):
            return nullcontext(records[int(name)])
        if parent != hive or access != 1 | view:
            raise PermissionError("unavailable registry view")
        assert name == r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        return nullcontext((parent, access))

    def enum_key(key, index):
        if index >= len(records):
            raise OSError("no more entries")
        return str(index)

    def query_value(entry, name):
        if name not in entry:
            raise FileNotFoundError(name)
        return entry[name], 1

    monkeypatch.setitem(
        sys.modules,
        "winreg",
        SimpleNamespace(
            HKEY_CURRENT_USER="user",
            HKEY_LOCAL_MACHINE="machine",
            KEY_READ=1,
            KEY_WOW64_64KEY=0x100,
            KEY_WOW64_32KEY=0x200,
            OpenKey=open_key,
            EnumKey=enum_key,
            QueryValueEx=query_value,
        ),
    )
    for name in ("COURSE2MD_DSH", "COURSE2MD_DSH_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("course2md.agents.dsh.DSH_DEFAULT_ROOTS", ())
    monkeypatch.setattr(shutil, "which", lambda name: None)

    assert dsh._dsh_registry_roots() == [str(root)]
    assert dsh.dsh_prefix() == [
        str(root / "DeepSeek Harness.exe"),
        "--expose-internals",
        str(root / dsh.DSH_CLI_RELATIVE),
    ]
    assert set(agent.detect_agents()) == {"dsh"}


def test_dsh_discovery_without_windows_registry(monkeypatch):
    monkeypatch.setitem(sys.modules, "winreg", None)
    assert dsh._dsh_registry_roots() == []


def test_codex_windows_npm_launcher_uses_node_with_separate_paths(tmp_path, monkeypatch):
    npm = tmp_path / "npm directory"
    script = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    script.parent.mkdir(parents=True)
    script.write_text("// launcher", encoding="utf-8")
    shim = npm / "codex.cmd"
    shim.write_text("@echo off", encoding="utf-8")
    monkeypatch.delenv("COURSE2MD_CODEX", raising=False)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: {
            "codex": str(shim),
            "node": "C:/Program Files/nodejs/node.exe",
        }.get(name),
    )
    assert codex.codex_prefix() == ["C:/Program Files/nodejs/node.exe", str(script)]


def test_codex_explicit_path_and_missing_path(tmp_path, monkeypatch):
    binary = tmp_path / "custom codex.exe"
    binary.write_bytes(b"fixture")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setenv("COURSE2MD_CODEX", str(binary))
    assert codex.codex_prefix() == [str(binary.resolve())]
    monkeypatch.setenv("COURSE2MD_CODEX", str(tmp_path / "missing.exe"))
    with pytest.raises(agent.AgentError, match="启动文件不存在"):
        codex.codex_prefix()
    monkeypatch.delenv("COURSE2MD_CODEX")
    assert codex.codex_prefix() is None


def test_codex_preset_and_detection_use_codex_launcher(monkeypatch):
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["fake-dsh"])
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["fake-codex"])
    command = agent.AgentConfig(preset="codex").resolved_command()
    assert command[:2] == ["fake-codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert "--output-last-message" in command
    assert set(agent.detect_agents()) == {"dsh", "codex"}


@pytest.fixture
def codex_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    script = tmp_path / "fake codex.py"
    script.write_text(
        """
import json
import sys
from pathlib import Path

sys.stdin.reconfigure(encoding='utf-8')
payload = sys.stdin.read()
Path('received.json').write_text(json.dumps({'stdin': payload, 'args': sys.argv[1:]}), encoding='utf-8')
output = sys.argv[sys.argv.index('--output-last-message') + 1]
content = Path('response.txt').read_text(encoding='utf-8')
Path(output).write_text(content, encoding='utf-8')
print('fixture finished')
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: [sys.executable, str(script)])
    return workspace


def test_codex_structured_call_passes_images_schema_and_effort(codex_workspace):
    (codex_workspace / "response.txt").write_text('{"topics": []}', encoding="utf-8")
    schema = codex_workspace / "lesson schema.json"
    schema.write_text("{}", encoding="utf-8")
    picture = codex_workspace / "课堂 picture.jpg"
    picture.write_bytes(b"image")
    response = agent.execute_agent(
        codex_workspace,
        agent.AgentConfig(preset="codex", reasoning_effort="medium", output_notes="lesson.json"),
        Mock(),
        instruction="Return the reply.",
        input_text="完整课程正文",
        images=[picture],
        schema=schema,
    )
    received = json.loads((codex_workspace / "received.json").read_text(encoding="utf-8"))
    args = received["args"]
    assert response == '{"topics": []}'
    assert args[args.index("--image") + 1] == str(picture.resolve())
    assert args[args.index("--output-schema") + 1] == str(schema.resolve())
    assert "model_reasoning_effort=medium" in args
    assert "完整课程正文" in received["stdin"]


def test_codex_receives_input_on_stdin_and_saves_final_reply(codex_workspace):
    (codex_workspace / "response.txt").write_text("整理结果", encoding="utf-8")
    reply = agent.execute_agent(
        codex_workspace,
        agent.AgentConfig(preset="codex", output_notes="reply.md"),
        Mock(),
        instruction="Return the reply.",
        input_text="对列与网络命令。",
    )
    received = json.loads((codex_workspace / "received.json").read_text(encoding="utf-8"))
    assert reply == "整理结果" and "对列与网络命令" in received["stdin"]
    assert received["args"][-1] == "-"
    assert received["args"][received["args"].index("--output-last-message") + 1] == "reply.md"
    assert "fixture finished" in (codex_workspace / "agent.log").read_text(encoding="utf-8")


def test_codex_empty_reply_is_an_error(codex_workspace):
    (codex_workspace / "response.txt").write_text("", encoding="utf-8")
    with pytest.raises(agent.AgentError, match="没有产出有效内容"):
        agent.execute_agent(
            codex_workspace,
            agent.AgentConfig(preset="codex"),
            Mock(),
            instruction="Return the reply.",
            input_text="正文",
        )


def test_codex_events_arrive_before_completion_and_resume_exact_session(tmp_path, monkeypatch):
    script = tmp_path / "stream.py"
    script.write_text(
        """
import json,sys,time
from pathlib import Path
sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':'thread-one'}),flush=True)
print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'read course','id':'a'}}),flush=True)
deadline=time.monotonic()+10
while not Path('ack').exists():
    if time.monotonic()>deadline: raise RuntimeError('missing ack')
    time.sleep(0.01)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text('{"done":true}',encoding='utf-8')
print(json.dumps({'type':'turn.completed'}),flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: [sys.executable, str(script)])
    events = []

    def observe(event):
        events.append(event)
        if event["kind"] == "tool":
            assert not (tmp_path / "reply.json").exists()
            (tmp_path / "ack").write_text("ready", encoding="utf-8")

    cfg = agent.AgentConfig(preset="codex", output_notes="reply.json", timeout=30)
    assert (
        agent.execute_agent(
            tmp_path, cfg, Mock(), instruction="Return the reply.", input_text="课程", on_output=observe
        )
        == '{"done":true}'
    )
    assert events[0]["session_id"] == "thread-one"
    assert json.loads((tmp_path / "agent-session.json").read_text())["session_id"] == "thread-one"
    (tmp_path / "reply.json").unlink()
    (tmp_path / "ack").unlink()
    agent.execute_agent(
        tmp_path,
        cfg,
        Mock(),
        instruction="Return the reply.",
        input_text="继续",
        on_output=observe,
        resume_id="thread-one",
    )
    assert events[-1]["text"] == "本轮输出完成"


def test_quiet_agent_cancels_without_waiting_for_more_stdout(tmp_path, monkeypatch):
    import threading

    script = tmp_path / "quiet.py"
    script.write_text(
        """
import sys,time,json
sys.stdin.read()
print(json.dumps({'type':'turn.started'}),flush=True)
time.sleep(120)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: [sys.executable, str(script)])
    cancel = threading.Event()
    with pytest.raises(agent.AgentError, match="取消"):
        agent.execute_agent(
            tmp_path,
            agent.AgentConfig(preset="codex", timeout=30),
            Mock(),
            cancel,
            instruction="Return the reply.",
            input_text="课程",
            on_output=lambda event: cancel.set(),
        )


def test_dsh_json_final_is_saved_and_private_reasoning_is_summarized(tmp_path, monkeypatch):
    script = tmp_path / "dsh.py"
    script.write_text(
        """
import sys,json
sys.stdin.read()
for item in [{'type':'session','sessionId':'session-one'},
             {'type':'thinking','text':'private analysis'},
             {'type':'text','text':'公开说明'},
             {'type':'final','text':'{"ok":true}'}]:
 print(json.dumps(item),flush=True)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: [sys.executable, str(script)])
    events = []
    response = agent.execute_agent(
        tmp_path,
        agent.AgentConfig(preset="dsh", output_notes="lesson.json"),
        Mock(),
        instruction="Return the reply.",
        input_text="课程",
        on_output=events.append,
    )
    assert response == '{"ok":true}'
    assert "private analysis" not in json.dumps(events)
    assert any(event["text"] == "公开说明" for event in events)


def test_agent_cancels_when_child_does_not_read_stdin(tmp_path, monkeypatch):
    import threading

    script = tmp_path / "blocked_input.py"
    script.write_text(
        """
import json,time
print(json.dumps({'type':'turn.started'}),flush=True)
time.sleep(120)
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: [sys.executable, str(script)])
    cancel = threading.Event()
    with pytest.raises(agent.AgentError, match="取消"):
        agent.execute_agent(
            tmp_path,
            agent.AgentConfig(preset="codex", timeout=30),
            Mock(),
            cancel,
            instruction="Return the reply.",
            input_text="x" * 2000000,
            on_output=lambda event: cancel.set(),
        )

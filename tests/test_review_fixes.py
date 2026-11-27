"""审查修复的回归测试：凭证脱敏、TLS、Agent 权限、配置解析、转写取消与对话续接。"""

from __future__ import annotations

import json
import subprocess
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from course2md import agents as agent
from course2md.agents import AgentConfig, AgentError, dsh, split_command
from course2md.asr.formats import Segment
from course2md.config import AsrConfig, LlmConfig, load_env_file
from course2md.stages.options import JobOptions


def test_tls_verification_is_on_unless_explicitly_disabled(monkeypatch):
    from course2md.resolvers.base import tls_verify

    monkeypatch.delenv("COURSE2MD_INSECURE_TLS", raising=False)
    assert tls_verify()
    monkeypatch.setenv("COURSE2MD_INSECURE_TLS", "1")
    assert not tls_verify()


def test_download_reports_certificate_failures(tmp_path):
    import requests

    from course2md.resolvers.base import MediaRef, ResolverError, download

    class Session:
        def get(self, *args, **kwargs):
            assert kwargs["verify"] is True
            raise requests.exceptions.SSLError("bad certificate")

    with pytest.raises(ResolverError, match="COURSE2MD_INSECURE_TLS"):
        download(MediaRef("https://cdn.example/v.mp4", "v.mp4"), tmp_path / "v.mp4", session=Session())


@pytest.mark.parametrize("structured", [True, False])
def test_dsh_runs_with_restricted_permissions(tmp_path, monkeypatch, structured):
    expected = ["--json", "--permission-mode", "read-only", "--approval-policy", "never"]
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["dsh"])
    captured = {}

    def popen(cmd, **kwargs):
        captured["cmd"] = cmd
        raise OSError("not started")

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(AgentError):
        agent.execute_agent(
            tmp_path,
            AgentConfig(preset="dsh"),
            lambda *_: None,
            instruction="Return the reply.",
            on_output=(lambda event: None) if structured else None,
        )
    command = captured["cmd"]
    assert all(item in command for item in expected)
    assert "danger-full-access" not in command
    assert command[-1] == "-" and command[1] == "headless"  # 指令与材料经标准输入传入


def test_split_command_keeps_quoted_windows_paths():
    assert split_command('"C:\\Program Files\\dsh\\dsh.exe" headless') == [
        "C:\\Program Files\\dsh\\dsh.exe",
        "headless",
    ]
    assert split_command("node C:\\tools\\cli.js --flag") == ["node", "C:\\tools\\cli.js", "--flag"]


def test_dsh_home_overrides_install_roots(monkeypatch):
    monkeypatch.setenv("COURSE2MD_DSH_HOME", "E:\\apps\\dsh")
    assert dsh._dsh_roots()[0] == "E:\\apps\\dsh"


def test_env_file_strips_export_prefix_only(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("export COURSE2MD_X_ONE=1\nproxy_token=2\nexported_name=3\n", encoding="utf-8")
    for key in ("COURSE2MD_X_ONE", "proxy_token", "exported_name"):
        monkeypatch.delenv(key, raising=False)
    load_env_file(env)
    import os

    assert (os.environ["COURSE2MD_X_ONE"], os.environ["proxy_token"], os.environ["exported_name"]) == ("1", "2", "3")
    for key in ("COURSE2MD_X_ONE", "proxy_token", "exported_name"):
        monkeypatch.delenv(key)


def test_transcription_stops_between_segments():
    from course2md.asr.whisper import WhisperBackend
    from course2md.stages import Cancelled

    produced = []

    def segments():
        for index in range(100):
            produced.append(index)
            yield SimpleNamespace(start=index, end=index + 0.9, text=f"第{index}句")

    backend = WhisperBackend(AsrConfig())
    backend._model = SimpleNamespace(transcribe=lambda *args, **kwargs: (segments(), None))
    cancel = threading.Event()

    def check():
        if len(produced) >= 3:
            cancel.set()
        if cancel.is_set():
            raise Cancelled("任务已取消")

    backend.check_cancel = check
    with pytest.raises(Cancelled):
        backend.transcribe(np.zeros(16000 * 120, dtype=np.float32))
    assert len(produced) == 3


def test_unknown_format_fails_before_any_work(tmp_path, monkeypatch):
    from course2md import pipeline
    from course2md.stages import media as media_stage

    monkeypatch.setattr(media_stage, "get_resolver", lambda source: pytest.fail("不应开始解析"))
    with pytest.raises(ValueError, match="docx"):
        pipeline.run_job(JobOptions(source="x.mp4", out_dir=tmp_path, formats=("srt", "docx")))


@pytest.fixture
def chat_workspace(tmp_path):
    from unittest.mock import Mock

    from course2md.frames.sampling import Frame
    from course2md.refine import lesson

    image = tmp_path / "画面.jpg"
    image.write_bytes(b"classroom-image")
    segments = [Segment(0, 4, "我们下周开始期中检查。"), Segment(60, 65, "例题选 B，这个是考试重点。")]
    data = {
        "overview": "消除左递归。",
        "topics": [
            {
                "title": "左递归",
                "timestamp": "00:01:00",
                "explanation": "引入新符号。",
                "key_points": ["保留 ε。"],
                "figure_ids": ["f1"],
            }
        ],
        "examples": [],
        "teacher_points": [{"evidence_id": "e1", "meaning": "下周期中检查。"}],
        "review_questions": ["为什么需要 ε？"],
        "uncertainties": [],
    }
    client = Mock()
    client.chat.side_effect = [
        json.dumps({"pages": [{"figure_id": "f1", "status": "readable", "content": "E→E+T|T"}]}),
        json.dumps(data),
    ]
    workspace = tmp_path / "work"
    pair = lesson.build_note_pair(
        segments, "课", {}, LlmConfig(vision=True), AgentConfig(), workspace, frames=[Frame(60, image)], client=client
    )
    detailed, _ = lesson.save_note_pair(pair, tmp_path / "课.notes.md")
    (workspace / "agent-session.json").write_text(json.dumps({"backend": "codex", "session_id": "s1"}))
    return workspace, detailed


def test_resumed_chat_sends_only_changing_context(chat_workspace, monkeypatch):
    from course2md.refine import conversation

    workspace, detailed = chat_workspace
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["codex"])
    calls = []

    def fake(directory, cfg, progress, cancel=None, **kwargs):
        calls.append(kwargs)
        return json.dumps({"reply": "选 B。", "lesson": None})

    monkeypatch.setattr(conversation, "execute_agent", fake)
    reply = conversation.continue_notes(
        workspace,
        "本题选什么？",
        [{"role": "user", "text": "旧问题"}],
        detailed,
        LlmConfig(),
        AgentConfig(preset="codex"),
    )
    assert reply.text == "选 B。" and len(calls) == 1
    sent = calls[0]["input_text"]
    assert calls[0]["resume_id"] == "s1"
    assert "本题选什么？" in sent and "当前课程记录" in sent
    assert "原始课程转写" not in sent and "旧问题" not in sent


def test_expired_session_falls_back_to_full_context(chat_workspace, monkeypatch):
    from course2md.refine import conversation

    workspace, detailed = chat_workspace
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["codex"])
    calls = []

    def fake(directory, cfg, progress, cancel=None, **kwargs):
        calls.append(kwargs)
        if kwargs["resume_id"]:
            raise AgentError("unknown session")
        return json.dumps({"reply": "选 B。", "lesson": None})

    monkeypatch.setattr(conversation, "execute_agent", fake)
    reply = conversation.continue_notes(
        workspace, "本题选什么？", [], detailed, LlmConfig(vision=True), AgentConfig(preset="codex")
    )
    assert reply.text == "选 B。"
    assert [call["resume_id"] for call in calls] == ["s1", None]
    assert "原始课程转写" in calls[1]["input_text"] and "E→E+T|T" in calls[1]["input_text"]
    assert not (workspace / "agent-session.json").exists()


def test_acl_probe_passes_for_normal_process(tmp_path):
    from course2md.util.winacl import acl_grant_problem

    assert acl_grant_problem(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


def test_acl_probe_reports_failure(tmp_path, monkeypatch):
    import os

    from course2md.util import winacl

    if os.name != "nt":
        pytest.skip("仅 Windows 检查 ACL")
    monkeypatch.setattr(
        winacl.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=5, stdout="拒绝访问。".encode("mbcs", errors="replace")),
    )
    assert winacl.acl_grant_problem(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_dsh_acl_failure_is_reported_once(tmp_path):
    import sys

    from course2md.util.winacl import ACL_FAILURE_MARKER, ACL_HINT

    line = json.dumps(
        {"type": "tool_result", "status": "error", "result": f"Error: sandbox-local: {ACL_FAILURE_MARKER}"}
    )
    script = (
        "import pathlib\n"
        f"print({line!r}, flush=True)\nprint({line!r}, flush=True)\n"
        "pathlib.Path('response.md').write_text('ok', encoding='utf-8')\n"
    )
    events, messages = [], []
    text = agent.execute_agent(
        tmp_path,
        AgentConfig(command=[sys.executable, "-c", script], timeout=60),
        lambda fraction, message: messages.append(message),
        instruction="Return the reply.",
        on_output=events.append,
    )
    assert text == "ok"
    assert [event["text"] for event in events if event["kind"] == "error"] == [ACL_HINT]
    assert ACL_HINT in (tmp_path / "agent.log").read_text(encoding="utf-8")

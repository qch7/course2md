"""DSH 与 Codex 的提供商、模型选择：配置来源、命令参数、网页传递与本机选项读取。"""

from __future__ import annotations

import json
import subprocess

import pytest

from course2md import agents as agent
from course2md.agents import AgentConfig, AgentError, codex, dsh
from course2md.agents import api as agent_api
from course2md.config import default_agent


def _command(tmp_path, monkeypatch, cfg: AgentConfig, **kwargs) -> list[str]:
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["dsh"])
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["codex"])
    captured = {}

    def popen(cmd, **_):
        captured["cmd"] = cmd
        raise OSError("not started")

    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(AgentError):
        agent.execute_agent(tmp_path, cfg, lambda *_: None, instruction="Return the reply.", **kwargs)
    return captured["cmd"]


def test_codex_receives_model_and_provider(tmp_path, monkeypatch):
    cmd = _command(
        tmp_path,
        monkeypatch,
        AgentConfig(preset="codex", provider="custom", model="gpt-6.1-sol"),
        on_output=lambda event: None,
        resume_id="thread-1",
    )
    position = cmd.index("exec")
    assert cmd[position + 1 : position + 5] == ["--model", "gpt-6.1-sol", "--config", 'model_provider="custom"']
    assert cmd[-3:] == ["resume", "thread-1", "-"]


@pytest.mark.parametrize("structured", [True, False])
def test_dsh_receives_model_patch(tmp_path, monkeypatch, structured):
    cmd = _command(
        tmp_path,
        monkeypatch,
        AgentConfig(preset="dsh", provider="newapi", model="grok-4.7"),
        on_output=(lambda event: None) if structured else None,
    )
    position = cmd.index("headless")
    assert cmd[position + 1] == "--patch"
    patch = (tmp_path / dsh.DSH_MODEL_PATCH).read_text(encoding="utf-8")
    assert cmd[position + 2] == str((tmp_path / dsh.DSH_MODEL_PATCH).resolve())
    assert patch == '- id: agent-default-model\n  config:\n    provider: "newapi"\n    model: "grok-4.7"\n'


def test_unset_choice_keeps_cli_defaults(tmp_path, monkeypatch):
    cmd = _command(tmp_path, monkeypatch, AgentConfig(preset="dsh"), on_output=lambda event: None)
    assert "--patch" not in cmd and not (tmp_path / dsh.DSH_MODEL_PATCH).exists()
    cmd = _command(tmp_path, monkeypatch, AgentConfig(preset="codex"), on_output=lambda event: None)
    assert "--model" not in cmd and not any("model_provider" in part for part in cmd)


def test_model_only_override_keeps_provider(tmp_path, monkeypatch):
    _command(tmp_path, monkeypatch, AgentConfig(preset="dsh", model="deepseek-v4-pro"), on_output=lambda e: None)
    assert "provider" not in (tmp_path / dsh.DSH_MODEL_PATCH).read_text(encoding="utf-8")


@pytest.mark.parametrize("value", ["bad name", "x\ny", 'a"b', "-flag"])
def test_unsafe_names_are_rejected(value):
    with pytest.raises(AgentError):
        AgentConfig(preset="dsh", model=value)


def test_environment_defaults_and_explicit_overrides(monkeypatch):
    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["dsh"])
    monkeypatch.setenv("COURSE2MD_DSH_PROVIDER", "newapi")
    monkeypatch.setenv("COURSE2MD_DSH_MODEL", "grok-4.7")
    monkeypatch.setenv("COURSE2MD_CODEX_MODEL", "gpt-6.1-sol")
    dsh = default_agent("dsh")
    assert (dsh.provider, dsh.model, dsh.label) == ("newapi", "grok-4.7", "newapi / grok-4.7")
    codex = default_agent("codex")
    assert (codex.provider, codex.model) == ("", "gpt-6.1-sol")
    explicit = default_agent("dsh", provider="", model="deepseek-flash")
    assert (explicit.provider, explicit.model) == ("", "deepseek-flash")
    assert default_agent("none").model == ""


DSH_DUMP = """# == @deepseek-ai/dsh-base
- id: agent-default-model
  name: '@deepseek-ai/dsh-agent-default-model'
  config:
    provider: deepseek-official
    model: deepseek-flash
- id: llm-pi-ai
  name: '@deepseek-ai/dsh-llm-pi-ai'
  config:
    providers:
      cc-switch-deep-seek:
        models:
          - name: DeepSeek V4.1 Flash
            input:
              - text
            id: deepseek-flash
        api: openai-completions
        baseURL: https://api.deepseek.com/v1
      newapi:
        models:
          - name: grok-4.7
            id: grok-4.7
          - id: gpt-6-astra
            name: gpt-6-astra
- id: agent-loop
  config:
    agents: []
"""


def test_parse_dsh_models():
    parsed = dsh.parse_dsh_models(DSH_DUMP)
    assert parsed["default_provider"] == "deepseek-official"
    assert parsed["default_model"] == "deepseek-flash"
    assert parsed["providers"] == {
        "cc-switch-deep-seek": ["deepseek-flash"],
        "newapi": ["grok-4.7", "gpt-6-astra"],
        "deepseek-official": ["deepseek-flash"],
    }
    assert dsh.parse_dsh_models("")["providers"] == {}


def test_parse_codex_models():
    pytest.importorskip("tomllib")
    parsed = codex.parse_codex_models(
        'model_provider = "custom"\nmodel = "gpt-6.1-sol"\n'
        '[model_providers.custom]\nname = "c"\nbase_url = "https://x"\n'
        '[profiles.fast]\nmodel = "gpt-6-mini"\n'
    )
    assert parsed["default_provider"] == "custom" and parsed["default_model"] == "gpt-6.1-sol"
    assert parsed["providers"]["custom"] == ["gpt-6.1-sol", "gpt-6-mini"]
    assert "openai" in parsed["providers"]
    assert codex.parse_codex_models("not = [toml")["providers"] == {}


def test_cli_rejects_unsafe_model(monkeypatch):
    from types import SimpleNamespace

    from course2md import cli

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["dsh"])
    with pytest.raises(SystemExit, match="模型名称无效"):
        cli._agent_from_args("dsh", SimpleNamespace(agent_provider=None, agent_model="bad name"))
    chosen = cli._agent_from_args("dsh", SimpleNamespace(agent_provider="newapi", agent_model="grok-4.7"))
    assert chosen.label == "newapi / grok-4.7"


def test_restored_options_keep_agent_choice():
    from dataclasses import asdict

    saved = json.loads(json.dumps(asdict(AgentConfig(preset="codex", provider="custom", model="m1"))))
    assert AgentConfig(**saved).label == "custom / m1"


def test_nested_cli_errors_are_readable():
    nested = {
        "message": json.dumps(
            {
                "type": "error",
                "status": 400,
                "error": {"type": "invalid_request_error", "message": "model not supported"},
            }
        )
    }
    assert agent_api.error_text(nested) == "model not supported"
    assert (
        agent.decode_output(json.dumps({"type": "error", "message": "plain"}), agent.get_preset("codex"))["text"]
        == "plain"
    )


def test_exit_message_names_choice_and_reason(tmp_path):
    import sys

    script = "import sys; print('dsh: UNKNOWN_MODEL: no model x'); sys.exit(1)"
    cfg = AgentConfig(command=[sys.executable, "-c", script], timeout=60, model="x")
    with pytest.raises(AgentError, match=r"Agent（默认提供商 / x） 退出码 1：dsh: UNKNOWN_MODEL"):
        agent.execute_agent(tmp_path, cfg, lambda *_: None, instruction="Return the reply.")

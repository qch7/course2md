"""扩展点约定：插件发现、Agent 预设插件、阶段自描述、业务错误还原。"""

from __future__ import annotations

import sys
from importlib.metadata import EntryPoint
from pathlib import Path
from types import ModuleType

import pytest

from course2md import agents
from course2md.agents import registry as agent_registry
from course2md.agents.api import AgentConfig, AgentError, AgentPreset, AgentRequest, Invocation
from course2md.errors import Course2mdError, rebuild_error
from course2md.plugins import PluginRegistry
from course2md.stages.base import BaseStage
from course2md.stages.codec import cache_key, output_model, requires_for
from course2md.stages.options import JobOptions
from course2md.stages.registry import PIPELINE_STAGES, STAGE_LABELS, STAGE_TYPES


@pytest.fixture
def entry_points(monkeypatch):
    """替换入口点列表：use(group, [(name, "module:attr"), ...])。"""
    groups: dict[str, list[EntryPoint]] = {}

    def use(group: str, items: list[tuple[str, str]]) -> None:
        groups[group] = [EntryPoint(name, value, group) for name, value in items]

    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: list(groups.get(group, [])))
    return use


@pytest.fixture
def plugin_module(monkeypatch):
    module = ModuleType("demo_course2md_plugin")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return module


def test_registry_keeps_builtins_and_rejects_conflicting_plugins(entry_points, plugin_module):
    plugin_module.Alpha = "alpha"
    plugin_module.Beta = "beta"
    entry_points(
        "demo.group",
        [
            ("alpha", "demo_course2md_plugin:Alpha"),
            ("builtin", "demo_course2md_plugin:Alpha"),  # 不能覆盖内置
            ("dup", "demo_course2md_plugin:Alpha"),
            ("dup", "demo_course2md_plugin:Beta"),  # 重名全部停用
        ],
    )
    registry: PluginRegistry[str] = PluginRegistry("演示插件", "demo.group", {"builtin": "demo_course2md_plugin:Beta"})
    assert registry.names() == ["builtin", "alpha"]
    assert registry.get("builtin") == "beta"
    assert registry.get("alpha") == "alpha"
    errors = registry.errors()
    assert any("内置名称「builtin」" in error for error in errors)
    assert sum("名称「dup」" in error for error in errors) == 2
    with pytest.raises(ValueError, match="未安装"):
        registry.get("dup")


def test_registry_validation_failure_is_reported(entry_points, plugin_module):
    plugin_module.Bad = 42
    entry_points("demo.group", [("bad", "demo_course2md_plugin:Bad")])
    registry: PluginRegistry[object] = PluginRegistry(
        "演示插件", "demo.group", {}, validate=lambda target: [] if callable(target) else ["须可调用"]
    )
    with pytest.raises(ValueError, match="须可调用"):
        registry.get("bad")


class EchoPreset(AgentPreset):
    """把标准输入原样写到输出文件的演示预设，用当前 Python 运行。"""

    name = "echo"
    label = "Echo"
    template = ("{prefix}", "-c", "import sys; open(sys.argv[1], 'w', encoding='utf-8').write(sys.stdin.read())")

    def locate(self) -> list[str] | None:
        return [sys.executable]

    def prepare(self, cmd: list[str], cfg: AgentConfig, request: AgentRequest) -> Invocation:
        return Invocation([*cmd, cfg.output_notes], f"{request.instruction}|{request.input_text}", {"PYTHONUTF8": "1"})


def test_third_party_agent_preset_runs_through_shared_runner(entry_points, plugin_module, tmp_path: Path):
    plugin_module.EchoPreset = EchoPreset
    entry_points(agent_registry.ENTRY_POINT_GROUP, [("echo", "demo_course2md_plugin:EchoPreset")])
    agent_registry.reload()
    try:
        assert agents.preset_names() == ["dsh", "codex", "echo"]
        cfg = AgentConfig(preset="echo")
        assert cfg.resolved_command()[0] == sys.executable
        text = agents.execute_agent(tmp_path, cfg, lambda *_: None, instruction="整理", input_text="课程")
        assert text == "整理|课程"
        assert requires_for("organize", JobOptions(source="x", out_dir=tmp_path, agent=cfg)) == ["agent:echo"]
    finally:
        agent_registry.reload()


def test_agent_preset_must_subclass_api(entry_points, plugin_module):
    plugin_module.NotPreset = object
    entry_points(agent_registry.ENTRY_POINT_GROUP, [("broken", "demo_course2md_plugin:NotPreset")])
    agent_registry.reload()
    try:
        with pytest.raises(AgentError, match="AgentPreset"):
            agents.get_preset("broken")
        assert AgentConfig(preset="broken").resolved_command() == []
    finally:
        agent_registry.reload()


def test_every_stage_declares_its_scheduling_contract():
    assert set(PIPELINE_STAGES) <= set(STAGE_TYPES)
    for name, stage in STAGE_TYPES.items():
        assert issubclass(stage, BaseStage)
        assert stage.name == name
        assert STAGE_LABELS[name] and STAGE_LABELS[name] != name, f"{name} 缺少中文展示名"
    anchors = [name for name, stage in STAGE_TYPES.items() if stage.anchors_node]
    assert anchors == ["media"]
    assert [name for name, stage in STAGE_TYPES.items() if stage.pinned_to_anchor] == ["frames"]


def test_cache_key_only_depends_on_declared_options(tmp_path):
    from course2md.stages.codec import options_to_dict

    base = JobOptions(source="lecture.mp4", out_dir=tmp_path)
    changed = JobOptions(source="lecture.mp4", out_dir=tmp_path, formats=("srt",), frame_interval=9.0)
    # output 只依赖 formats，proofread 不依赖 formats 与 frame_interval
    assert cache_key("proofread", options_to_dict(base), None) == cache_key("proofread", options_to_dict(changed), None)
    assert cache_key("output", options_to_dict(base), None) != cache_key("output", options_to_dict(changed), None)
    assert output_model("proofread", base) == base.llm.model
    assert output_model("unknown-stage", base) == ""


def test_pinned_stage_fallback_adds_source_capabilities(tmp_path):
    from course2md.stages.frames import FramesStage

    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"video")
    options = JobOptions(source=str(video), out_dir=tmp_path)
    assert FramesStage.requires(options) == ["frames"]
    assert FramesStage.fallback_requires(options) == ["frames", "hub", "resolver:local"]


def test_business_errors_round_trip_by_name():
    from course2md.refine.llm import LlmError
    from course2md.resolvers import ResolverError

    for cls in (AgentError, LlmError, ResolverError):
        assert issubclass(cls, Course2mdError)
        rebuilt = rebuild_error(cls.__name__, "原因")
        assert type(rebuilt) is cls and str(rebuilt) == "原因"
    assert type(rebuild_error("ValueError", "x")) is ValueError
    assert rebuild_error("SomethingElse", "x") is None


class SlowStartPreset(AgentPreset):
    """启动时记录时间，0.4 秒后才输出第一行，用来检查 CLI 启动阶段是否串行。"""

    name = "slowstart"
    template = (
        "{prefix}",
        "-c",
        "import sys,time; print(time.time(), file=open(sys.argv[1], 'a')); time.sleep(0.4); print('ready', flush=True); "
        "open(sys.argv[2],'w',encoding='utf-8').write('ok')",
    )

    def locate(self) -> list[str] | None:
        return [sys.executable]

    def prepare(self, cmd: list[str], cfg: AgentConfig, request: AgentRequest) -> Invocation:
        return Invocation([*cmd, str(request.workspace.parent / "starts.txt"), cfg.output_notes], "")


def test_agent_startup_is_serialized(entry_points, plugin_module, tmp_path: Path):
    import threading

    plugin_module.SlowStartPreset = SlowStartPreset
    entry_points(agent_registry.ENTRY_POINT_GROUP, [("slowstart", "demo_course2md_plugin:SlowStartPreset")])
    agent_registry.reload()
    try:
        cfg = AgentConfig(preset="slowstart")
        threads = [
            threading.Thread(
                target=agents.execute_agent,
                args=(tmp_path / f"run-{index}", cfg, lambda *_: None),
                kwargs={"instruction": "x"},
            )
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        starts = sorted(float(line) for line in (tmp_path / "starts.txt").read_text().split())
        assert len(starts) == 2 and starts[1] - starts[0] >= 0.35
    finally:
        agent_registry.reload()

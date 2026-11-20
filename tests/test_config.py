"""命令行和环境配置优先级。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from course2md import cli, config, pipeline


@pytest.mark.parametrize("choice", ["none", "builtin", ""])
def test_explicit_builtin_agent_keeps_tuning_without_launch_command(monkeypatch, choice):
    monkeypatch.setenv("COURSE2MD_AGENT", "dsh")
    monkeypatch.setenv("COURSE2MD_AGENT_TIMEOUT", "420")
    agent = config.default_agent(choice)
    assert agent.resolved_command() == []
    assert agent.timeout == 420


def test_environment_none_selects_builtin(monkeypatch):
    monkeypatch.setenv("COURSE2MD_AGENT", "none")
    assert config.default_agent().resolved_command() == []


def test_explicit_model_precedes_environment(monkeypatch):
    monkeypatch.setenv("COURSE2MD_WHISPER_MODEL", "environment-model")
    assert config.find_model("explicit-model") == "explicit-model"
    assert config.find_model() == "environment-model"


def test_run_keeps_environment_tuning_and_explicit_cpu_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURSE2MD_DEVICE", "cuda")
    monkeypatch.setenv("COURSE2MD_COMPUTE_TYPE", "int8_float16")
    monkeypatch.setenv("COURSE2MD_WHISPER_MODEL", "environment-model")
    monkeypatch.setenv("COURSE2MD_LANGUAGE", "en")
    monkeypatch.setenv("COURSE2MD_BEAM_SIZE", "7")
    monkeypatch.setenv("COURSE2MD_CHUNK_CHARS", "4321")
    monkeypatch.setenv("COURSE2MD_DETAIL", "0")
    monkeypatch.setenv("COURSE2MD_FRAMES", "0")
    monkeypatch.setenv("COURSE2MD_FRAME_SELECTOR", "classroom")
    args = cli.build_parser().parse_args(
        [
            "run",
            "lecture.mp4",
            "--device",
            "cpu",
            "--model",
            "explicit-model",
            "--agent",
            "dsh",
            "--frame-selector",
            "general",
        ]
    )
    result = pipeline.JobResult(
        output_dir=tmp_path,
        transcripts_by_media={
            "one": {"srt": tmp_path / "one.srt"},
            "two": {"srt": tmp_path / "two.srt"},
        },
    )
    run = Mock(return_value=result)
    monkeypatch.setattr(pipeline, "run_job", run)

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["fake-dsh"])
    assert cli.cmd_run(args) == 0
    options = run.call_args.args[0]
    assert options.asr.model == "explicit-model"
    assert options.asr.device == "cpu"
    assert options.asr.compute_type == "int8"
    assert options.asr.language == "en"
    assert options.asr.beam_size == 7
    assert options.llm.chunk_chars == 4321
    assert (options.detail, options.frames) == (False, False)
    assert options.frame_selector == "general"
    output = capsys.readouterr().out
    assert "one.srt" in output and "two.srt" in output


def test_codex_environment_selection_keeps_runtime_tuning(monkeypatch):
    monkeypatch.setenv("COURSE2MD_AGENT", "codex")
    monkeypatch.setenv("COURSE2MD_AGENT_TIMEOUT", "300")
    chosen = config.default_agent()
    assert chosen.preset == "codex"
    assert chosen.timeout == 300


@pytest.mark.parametrize("backend", ["codex", "none"])
def test_refine_writes_stage_timings(tmp_path, monkeypatch, backend):
    from course2md.asr.formats import Segment, to_srt
    from course2md.refine import lesson

    transcript = tmp_path / "lecture.srt"
    transcript.write_text(to_srt([Segment(0, 2, "课堂内容")]), encoding="utf-8")
    output = tmp_path / "nested" / "notes.md"
    args = cli.build_parser().parse_args(
        [
            "refine",
            str(transcript),
            "--agent",
            backend,
            "-o",
            str(output),
            "--no-frames",
        ]
    )
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["fake-codex"])
    monkeypatch.setattr(
        lesson,
        "build_note_pair",
        lambda *args, **kwargs: lesson.NotePair("# 课程笔记\n", "# 缩略版\n", timings={"organize": 3}),
    )
    ticks = iter([10, 15])
    monkeypatch.setattr(cli, "time", SimpleNamespace(perf_counter=lambda: next(ticks)))
    assert cli.cmd_refine(args) == 0
    import json

    record = json.loads(output.with_suffix(".timings.json").read_text(encoding="utf-8"))
    assert record["backend"] == ("codex" if backend == "codex" else "builtin")
    assert record["segments"] == 1
    assert record["model_calls"] == 1
    assert record["images_used"] == 0
    assert record["timings_seconds"] == {"organize": 3, "total": 5}
    assert output.read_text(encoding="utf-8") == "# 课程笔记\n"
    assert "缩略版" in output.with_name("notes.brief.md").read_text(encoding="utf-8")

"""统一配置：Schema、配置文件、生成文件与优先级。"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from course2md.config import AsrConfig, LlmConfig, load_config_file, load_env_file, validate_environment
from course2md.config.files import GENERATED, ConfigError, read_config_file
from course2md.config.schema import BY_ENV, SETTINGS

ROOT = Path(__file__).resolve().parents[2]
# 不属于用户配置的变量：配置文件路径本身、测试开关
NOT_IN_SCHEMA = {"COURSE2MD_CONFIG", "COURSE2MD_TEST_GPU"}


def test_generated_files_are_up_to_date():
    for relative, render in GENERATED.items():
        assert (ROOT / relative).read_text(encoding="utf-8") == render(), (
            f"{relative} 已过期，运行 python scripts/gen_config.py"
        )


def test_every_variable_read_by_code_is_documented():
    pattern = re.compile(r'"(COURSE2MD_[A-Z0-9_]+)"')
    used = set()
    for path in (ROOT / "src" / "course2md").rglob("*.py"):
        used |= set(pattern.findall(path.read_text(encoding="utf-8")))
    missing = used - set(BY_ENV) - NOT_IN_SCHEMA
    assert not missing, f"配置 Schema 缺少：{sorted(missing)}"


def test_settings_have_unique_keys():
    keys = [(item.section, item.key) for item in SETTINGS]
    assert len(keys) == len(set(keys))


def test_config_file_fills_unset_variables_only(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(
        '[llm]\nmodel = "from-file"\nchunk_chars = 900\n'
        '[asr]\ndevice = "cpu"\n[output]\nformats = ["srt", "json"]\n'
        "[notes]\nframes = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("COURSE2MD_LLM_MODEL", "from-env")
    for name in (
        "COURSE2MD_CHUNK_CHARS",
        "COURSE2MD_DEVICE",
        "COURSE2MD_FORMATS",
        "COURSE2MD_FRAMES",
        "COURSE2MD_COMPUTE_TYPE",
    ):
        monkeypatch.delenv(name, raising=False)
    assert load_config_file(path) == path
    llm = LlmConfig.from_env()
    assert llm.model == "from-env"  # 环境变量优先于配置文件
    assert llm.chunk_chars == 900
    assert AsrConfig.from_env().compute_type == "int8"  # cpu 默认精度
    assert os.environ["COURSE2MD_FORMATS"] == "srt,json"
    assert os.environ["COURSE2MD_FRAMES"] == "0"


def test_dotenv_takes_priority_over_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("COURSE2MD_LLM_MODEL", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("COURSE2MD_LLM_MODEL=from-dotenv\n", encoding="utf-8")
    config = tmp_path / "config.toml"
    config.write_text('[llm]\nmodel = "from-file"\n', encoding="utf-8")
    load_env_file(dotenv)
    load_config_file(config)
    assert LlmConfig.from_env().model == "from-dotenv"


@pytest.mark.parametrize(
    "content, message",
    [
        ("[nope]\nx = 1\n", "未知的配置表"),
        ("[llm]\nmodle = 'x'\n", "没有配置项 modle"),
        ("[llm\n", "不是有效的 TOML"),
    ],
)
def test_config_file_errors_are_reported(tmp_path, content, message):
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        read_config_file(path)


def test_validate_environment_reports_bad_values():
    errors = validate_environment(
        {
            "COURSE2MD_CHUNK_CHARS": "lots",
            "COURSE2MD_DETAIL": "maybe",
            "COURSE2MD_DEVICE": "tpu",
            "COURSE2MD_LLM_BASE": "ftp://x",
            "COURSE2MD_FRAME_INTERVAL": "2.5",
            "UNRELATED": "x",
        }
    )
    assert len(errors) == 4
    assert any("COURSE2MD_CHUNK_CHARS" in item for item in errors)
    assert not any("FRAME_INTERVAL" in item for item in errors)


def test_example_toml_parses_when_uncommented(tmp_path):
    from course2md.config.files import render_example_toml

    lines = [line[2:] if re.match(r"# [a-z_]+ = ", line) else line for line in render_example_toml().splitlines()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    values = read_config_file(path)
    assert values["COURSE2MD_LLM_BASE"] == "https://api.deepseek.com/v1"
    assert values["COURSE2MD_FORMATS"] == "srt,txt,md"
    assert not validate_environment(values)

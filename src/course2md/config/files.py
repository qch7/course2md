"""配置文件：读取 TOML，并由 :mod:`.schema` 生成示例文件与文档。"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from .schema import SECTIONS, SETTINGS, Setting, to_env_value

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

CONFIG_ENV = "COURSE2MD_CONFIG"
_BY_KEY = {(item.section, item.key): item for item in SETTINGS}


def user_config_dir() -> Path:
    from platformdirs import user_config_dir as _dir

    return Path(_dir("course2md", appauthor=False))


def config_path() -> Path:
    """配置文件位置：COURSE2MD_CONFIG，否则为用户配置目录下的 config.toml。"""
    explicit = os.environ.get(CONFIG_ENV, "").strip()
    return Path(explicit) if explicit else user_config_dir() / "config.toml"


class ConfigError(ValueError):
    """配置文件格式或取值错误。"""


def read_config_file(path: Path) -> dict[str, str]:
    """读取 TOML 配置，返回 环境变量名 → 文本值。未知的表或键报错，便于发现拼写错误。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} 不是有效的 TOML：{exc}") from exc
    values: dict[str, str] = {}
    for section, table in data.items():
        if section not in SECTIONS or not isinstance(table, dict):
            raise ConfigError(f"{path}：未知的配置表 [{section}]，可选 {', '.join(SECTIONS)}")
        for key, value in table.items():
            setting = _BY_KEY.get((section, key))
            if setting is None:
                raise ConfigError(f"{path}：[{section}] 中没有配置项 {key}")
            values[setting.env] = to_env_value(setting, value)
    return values


def load_config_file(path: Path | None = None) -> Path | None:
    """把配置文件中的值写入 os.environ，已经设置的环境变量不覆盖。文件不存在时跳过。"""
    target = path or config_path()
    if not target.is_file():
        return None
    for name, value in read_config_file(target).items():
        if name not in os.environ:
            os.environ[name] = value
    return target


# ------------------------------------------------------------------ 生成
def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _describe(setting: Setting) -> str:
    text = setting.description
    if setting.choices:
        text += f"可选：{', '.join(setting.choices)}。"
    return text


def render_example_toml() -> str:
    lines = [
        "# course2md 配置文件示例（由 scripts/gen_config.py 生成，请勿手改）",
        "# 复制到用户配置目录下的 config.toml（course2md doctor 会显示位置），",
        "# 或用环境变量 COURSE2MD_CONFIG 指定路径。",
        "# 优先级：命令行参数 > 环境变量（含 .env）> 本文件 > 默认值。",
        "# 所有项都已注释，取消注释后生效。",
    ]
    for section, title in SECTIONS.items():
        lines += ["", f"# ---- {title}", f"[{section}]"]
        for setting in (item for item in SETTINGS if item.section == section):
            lines.append(f"# {_describe(setting)}（环境变量 {setting.env}）")
            if setting.example:
                lines.append(f"# 例：{setting.key} = {_toml_value(setting.example)}")
            lines.append(f"# {setting.key} = {_toml_value(setting.default)}")
    return "\n".join(lines) + "\n"


def render_env_example() -> str:
    lines = [
        "# course2md 环境变量模板（由 scripts/gen_config.py 生成，请勿手改）",
        "# 复制为 .env 后填写；.env 不会进入版本库。",
        "# 优先级：命令行参数 > 环境变量（含 .env）> 配置文件 > 默认值。",
    ]
    for section, title in SECTIONS.items():
        lines += ["", f"# ======== {title}"]
        for setting in (item for item in SETTINGS if item.section == section):
            lines.append(f"# {_describe(setting)}")
            if setting.example:
                lines.append(f"# 例：{setting.env}={to_env_value(setting, setting.example)}")
            default = "" if setting.secret else to_env_value(setting, setting.default)
            lines.append(f"{setting.env}={default}")
    return "\n".join(lines) + "\n"


def render_docs_table() -> str:
    lines = [
        "# 配置项",
        "",
        "> 本页由 `scripts/gen_config.py` 根据 `course2md.config.schema` 生成，请勿手改。",
        "",
        "每一项既可以写成环境变量（或写进 `.env`），也可以写进配置文件 `config.toml` 的对应表中。",
        "优先级：命令行参数 > 环境变量（含 `.env`）> 配置文件 > 默认值。",
        "配置文件默认位于用户配置目录（`course2md doctor` 会显示），也可以用 `COURSE2MD_CONFIG` 指定。",
    ]
    for section, title in SECTIONS.items():
        lines += [
            "",
            f"## {title}",
            "",
            f"配置文件中的表：`[{section}]`",
            "",
            "| 环境变量 | 配置键 | 默认值 | 说明 |",
            "|---|---|---|---|",
        ]
        for setting in (item for item in SETTINGS if item.section == section):
            default = to_env_value(setting, setting.default) if setting.default not in ("", [], None) else "（空）"
            lines.append(f"| `{setting.env}` | `{setting.key}` | `{default}` | {_describe(setting)} |")
    return "\n".join(lines) + "\n"


GENERATED = {
    "config.example.toml": render_example_toml,
    ".env.example": render_env_example,
    "docs/configuration.md": render_docs_table,
}

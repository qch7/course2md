"""运行时配置：.env 与配置文件加载、CUDA 运行库、模型定位、LLM 与转写参数。

全部配置项定义在 :mod:`.schema`。优先级：命令行参数 > 环境变量（含 .env）> 配置文件 > 默认值。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from ..frame_selectors.api import DEFAULT_SELECTOR
from .files import ConfigError, config_path, load_config_file, user_config_dir
from .schema import SETTINGS, validate_environment

MODEL_REPO = "models--Systran--faster-whisper-{name}"
DEFAULT_HF_CACHE = "hf-cache"
DEFAULT_LLM_BASE = "https://api.deepseek.com/v1"

__all__ = [
    "SETTINGS",
    "AsrConfig",
    "ConfigError",
    "LlmConfig",
    "bootstrap_runtime",
    "config_path",
    "default_agent",
    "env",
    "find_cuda_dirs",
    "find_model",
    "load_config_file",
    "load_env_file",
    "load_settings",
    "user_config_dir",
    "validate_environment",
]


# --------------------------------------------------------------------- .env
def load_env_file(path: Path | None = None) -> Path | None:
    """读取 .env 并写入 os.environ（已存在的变量不覆盖）。

    查找顺序：显式路径 → 当前工作目录 → 用户配置目录。找不到就静默跳过。
    """
    candidates = [path] if path else [Path.cwd() / ".env", user_config_dir() / ".env"]
    for candidate in candidates:
        if candidate is None or not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith(("export ", "export\t")):
                key = key[len("export") :].strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
        return candidate
    return None


def load_settings() -> tuple[Path | None, Path | None]:
    """依次读取 .env 与配置文件（前者优先），返回实际读取的两个路径。"""
    return load_env_file(), load_config_file()


def env(name: str, default: str = "") -> str:
    """读环境变量，空字符串视为未设置。"""
    return os.environ.get(name) or default


# ------------------------------------------------------------- CUDA 运行库
def find_cuda_dirs() -> list[Path]:
    """查找以 nvidia-cublas-cu12 / nvidia-cudnn-cu12 安装的运行库目录。"""
    dirs: list[Path] = []
    for entry in sys.path:
        root = Path(entry or ".") / "nvidia"
        for sub in ("cublas", "cudnn"):
            for name in ("bin", "lib"):
                candidate = root / sub / name
                if candidate.is_dir() and candidate not in dirs:
                    dirs.append(candidate)
    return dirs


def bootstrap_runtime() -> list[Path]:
    """让 CTranslate2 能找到 pip 安装的 cuBLAS / cuDNN（Windows 需显式登记 DLL 目录）。"""
    dirs = find_cuda_dirs()
    for dll_dir in dirs:
        try:
            os.add_dll_directory(str(dll_dir))
        except (AttributeError, OSError):
            pass
        if str(dll_dir) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
    return dirs


def find_model(name: str | None = None) -> str:
    """接受本地目录或模型名；是模型名时优先用已下载的本地副本，避免联网。"""
    value = name if name is not None else (env("COURSE2MD_WHISPER_MODEL") or "large-v3")
    if Path(value).is_dir():
        return value

    repo = MODEL_REPO.format(name=value)
    roots: list[Path] = []
    hf_home = env("HF_HOME")
    if hf_home:
        roots += [Path(hf_home) / "hub", Path(hf_home)]
    local = Path.cwd() / DEFAULT_HF_CACHE
    roots += [local / "hub", local, Path.home() / ".cache" / "huggingface" / "hub"]
    for root in roots:
        snapshots = root / repo / "snapshots"
        if snapshots.is_dir():
            for snap in sorted(snapshots.iterdir(), reverse=True):
                if (snap / "model.bin").is_file():
                    return str(snap)
    return value


# -------------------------------------------------------------------- 配置
@dataclass
class AsrConfig:
    """转写参数。默认值来自实测：长音频必须关闭上下文串联，否则一句幻觉会污染全片。"""

    model: str = "large-v3"
    device: str = "cuda"
    compute_type: str = "int8_float16"
    language: str = "zh"
    beam_size: int = 5
    vad: bool = True
    initial_prompt: str = ""
    gap_repair_seconds: float = 3.0
    leading_skip_seconds: float = 0.0
    cpu_threads: int = 0

    @classmethod
    def from_env(cls) -> AsrConfig:
        device = env("COURSE2MD_DEVICE", "cuda")
        default_compute = "int8_float16" if device == "cuda" else "int8"
        return cls(
            model=env("COURSE2MD_WHISPER_MODEL") or "large-v3",
            device=device,
            compute_type=env("COURSE2MD_COMPUTE_TYPE", default_compute),
            language=env("COURSE2MD_LANGUAGE", "zh"),
            beam_size=int(env("COURSE2MD_BEAM_SIZE", "5")),
        )


@dataclass
class LlmConfig:
    """OpenAI 兼容接口。"""

    base_url: str = DEFAULT_LLM_BASE
    model: str = "deepseek-chat"
    api_key: str = ""
    temperature: float = 0.3
    max_tokens: int = 8000
    timeout: int = 180
    retries: int = 3
    chunk_chars: int = 12000
    max_images: int = 12  # 单批读取上限，全课画面总数不限
    screen_threshold: int = 40  # 去重后画面超过该张数时先用缩略图初筛，0 关闭
    vision_concurrency: int = 3  # 初筛各轮、读原图各批同时发出的请求数
    vision: bool = False

    def resolved_base_url(self) -> str:
        """接口地址；环境变量在构造时已经作为默认值吸收，这里只返回最终值。"""
        return self.base_url

    def resolved_key(self) -> str:
        return self.api_key or env("COURSE2MD_API_KEY") or env("DEEPSEEK_API_KEY") or env("OPENAI_API_KEY")

    @classmethod
    def from_env(cls) -> LlmConfig:
        return cls(
            base_url=env("COURSE2MD_LLM_BASE", DEFAULT_LLM_BASE),
            model=env("COURSE2MD_LLM_MODEL", "deepseek-chat"),
            chunk_chars=int(env("COURSE2MD_CHUNK_CHARS", "12000")),
            max_images=int(env("COURSE2MD_MAX_IMAGES", "12")),
            screen_threshold=int(env("COURSE2MD_SCREEN_THRESHOLD", "40")),
            vision_concurrency=max(1, int(env("COURSE2MD_VISION_CONCURRENCY", "3"))),
            vision=env("COURSE2MD_LLM_VISION", "0") in ("1", "true", "yes"),
        )


def default_agent(choice: str | None = None, provider: str | None = None, model: str | None = None):
    """选择 Agent 并保留环境中的运行参数。None 自动选择，空值或 none 使用内置整理。

    provider 与 model 为 None 时读取 COURSE2MD_DSH_* 或 COURSE2MD_CODEX_*，空字符串沿用 CLI 自身配置。
    """
    from ..agents import AgentConfig, preset_names, split_command
    from ..agents.registry import AUTO_PRESET, find_preset

    raw = (env("COURSE2MD_AGENT") if choice is None else choice).strip()
    timeout = int(env("COURSE2MD_AGENT_TIMEOUT", "1800"))
    effort = env("COURSE2MD_CODEX_EFFORT", "medium")
    if not raw and choice is None:
        preset = find_preset(AUTO_PRESET)
        raw = AUTO_PRESET if preset and preset.locate() else "none"
    if raw in ("", "none", "builtin"):
        return AgentConfig(timeout=timeout, reasoning_effort=effort)
    if raw in preset_names():
        prefix = f"COURSE2MD_{raw.upper()}"
        return AgentConfig(
            preset=raw,
            timeout=timeout,
            reasoning_effort=effort,
            provider=env(f"{prefix}_PROVIDER") if provider is None else provider,
            model=env(f"{prefix}_MODEL") if model is None else model,
        )
    return AgentConfig(command=split_command(raw), timeout=timeout, reasoning_effort=effort)


def default_out_dir() -> str:
    return env("COURSE2MD_OUT_DIR", "course2md-output")


def default_term_start() -> str:
    """学期第 1 周的起始日（YYYY-MM-DD）；留空则按课次日期推断。"""
    return env("COURSE2MD_TERM_START", "")


def default_detail() -> bool:
    return env("COURSE2MD_DETAIL", "1") not in ("0", "false", "no")


def default_frames() -> bool:
    return env("COURSE2MD_FRAMES", "1") not in ("0", "false", "no")


def default_delete_video() -> bool:
    """网页默认在成功提取后清理本任务下载的视频。"""
    return env("COURSE2MD_DELETE_VIDEO", "1") not in ("0", "false", "no")


def default_frame_interval() -> float:
    return float(env("COURSE2MD_FRAME_INTERVAL", "5"))


def default_frame_selector() -> str:
    return env("COURSE2MD_FRAME_SELECTOR").strip() or DEFAULT_SELECTOR


def default_formats() -> tuple[str, ...]:
    raw = env("COURSE2MD_FORMATS", "srt,txt,md")
    return tuple(part.strip() for part in raw.split(",") if part.strip())

"""全部配置项的唯一定义。

``config.example.toml``、``.env.example`` 与 ``docs/configuration.md`` 都由本表生成
（``python scripts/gen_config.py``），测试会检查三者与本表一致。

每一项同时对应一个环境变量和配置文件中的 ``[section] key``。取值优先级：
命令行参数 > 环境变量（含 .env）> 配置文件 > 默认值。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import TypeAdapter, ValidationError

Kind = Literal["str", "int", "float", "bool", "list", "path", "url", "secret"]


@dataclass(frozen=True)
class Setting:
    env: str
    section: str
    key: str
    default: Any
    kind: Kind
    description: str
    choices: tuple[str, ...] = ()
    example: str = ""  # 只出现在示例文件注释中的示例值

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


SECTIONS: dict[str, str] = {
    "llm": "大模型接口（校对、长课证据与内置整理）",
    "agent": "整理后端（DSH / Codex / 内置）",
    "asr": "转写（faster-whisper）",
    "notes": "笔记与画面",
    "output": "输出",
    "resolvers": "来源解析插件",
    "runtime": "运行环境",
    "server": "网页服务与安全",
    "deploy": "服务器部署（course2md server）",
    "worker": "计算节点",
}

SETTINGS: tuple[Setting, ...] = (
    # ------------------------------------------------------------------ llm
    Setting(
        "COURSE2MD_API_KEY",
        "llm",
        "api_key",
        "",
        "secret",
        "OpenAI 兼容接口的 API Key。也可用 DEEPSEEK_API_KEY 或 OPENAI_API_KEY。",
    ),
    Setting(
        "COURSE2MD_LLM_BASE",
        "llm",
        "base",
        "https://api.deepseek.com/v1",
        "url",
        "OpenAI 兼容接口地址，例如 https://api.openai.com/v1、http://127.0.0.1:11434/v1（Ollama）。",
    ),
    Setting("COURSE2MD_LLM_MODEL", "llm", "model", "deepseek-chat", "str", "内置整理与校对使用的模型名。"),
    Setting(
        "COURSE2MD_CHUNK_CHARS", "llm", "chunk_chars", 12000, "int", "每次送给模型的最大字符数，课程过长时自动分块。"
    ),
    Setting("COURSE2MD_LLM_VISION", "llm", "vision", False, "bool", "内置接口所用模型支持图像输入时开启。"),
    # ---------------------------------------------------------------- agent
    Setting(
        "COURSE2MD_AGENT",
        "agent",
        "backend",
        "",
        "str",
        "整理后端：dsh、codex、none（内置单次调用）或自定义命令；留空时装了 DSH 就用 DSH。",
        example="dsh",
    ),
    Setting("COURSE2MD_DSH", "agent", "dsh_command", "", "str", "DSH 启动命令，留空自动探测桌面版或 PATH。"),
    Setting("COURSE2MD_DSH_HOME", "agent", "dsh_home", "", "path", "DeepSeek Harness 桌面版安装目录，留空自动探测。"),
    Setting(
        "COURSE2MD_DSH_PROVIDER",
        "agent",
        "dsh_provider",
        "",
        "str",
        "DSH 使用的提供商（须已在 DSH 中登记），留空沿用 DSH 默认设置。",
    ),
    Setting("COURSE2MD_DSH_MODEL", "agent", "dsh_model", "", "str", "DSH 使用的模型，留空沿用 DSH 默认设置。"),
    Setting("COURSE2MD_CODEX", "agent", "codex_command", "", "path", "Codex CLI 启动文件，留空从 PATH 探测。"),
    Setting(
        "COURSE2MD_CODEX_PROVIDER",
        "agent",
        "codex_provider",
        "",
        "str",
        "Codex 使用的提供商（须已在 ~/.codex/config.toml 登记），留空沿用其默认设置。",
    ),
    Setting("COURSE2MD_CODEX_MODEL", "agent", "codex_model", "", "str", "Codex 使用的模型，留空沿用其默认设置。"),
    Setting(
        "COURSE2MD_CODEX_EFFORT",
        "agent",
        "codex_effort",
        "medium",
        "str",
        "Codex 推理强度。",
        choices=("minimal", "low", "medium", "high", "xhigh"),
    ),
    Setting("COURSE2MD_AGENT_TIMEOUT", "agent", "timeout", 1800, "int", "Agent 单次任务超时（秒）。"),
    # ------------------------------------------------------------------ asr
    Setting(
        "COURSE2MD_WHISPER_MODEL",
        "asr",
        "model",
        "large-v3",
        "str",
        "faster-whisper 模型名（large-v3 / medium / small）或本地模型目录。",
    ),
    Setting("COURSE2MD_DEVICE", "asr", "device", "cuda", "str", "转写设备。", choices=("cuda", "cpu")),
    Setting(
        "COURSE2MD_COMPUTE_TYPE",
        "asr",
        "compute_type",
        "",
        "str",
        "计算精度，留空时 GPU 用 int8_float16、CPU 用 int8。",
        example="int8_float16",
    ),
    Setting("COURSE2MD_LANGUAGE", "asr", "language", "zh", "str", "转写语言。"),
    Setting("COURSE2MD_BEAM_SIZE", "asr", "beam_size", 5, "int", "解码束宽。"),
    # ---------------------------------------------------------------- notes
    Setting(
        "COURSE2MD_DETAIL", "notes", "detail", True, "bool", "首选展示详细版笔记；关闭时首选缩略版，两版均会生成。"
    ),
    Setting("COURSE2MD_FRAMES", "notes", "frames", True, "bool", "筛选并保存课件画面，供整理与 Markdown 配图使用。"),
    Setting(
        "COURSE2MD_FRAME_INTERVAL",
        "notes",
        "frame_interval",
        5.0,
        "float",
        "画面采样间隔（秒），全课扫描最高使用 1 秒。",
    ),
    Setting(
        "COURSE2MD_FRAME_SELECTOR",
        "notes",
        "frame_selector",
        "classroom",
        "str",
        "画面筛选策略：classroom（教室投影）、general（整幅画面变化）或已安装的插件名称。",
    ),
    Setting("COURSE2MD_MAX_IMAGES", "notes", "max_images", 12, "int", "每批最多读取的截图数，整课画面总数不限。"),
    Setting(
        "COURSE2MD_SCREEN_THRESHOLD",
        "notes",
        "screen_threshold",
        40,
        "int",
        "去重后的画面超过该张数时，先用缩略图总览初筛，只读取入选画面的原图；0 表示关闭初筛。",
    ),
    Setting(
        "COURSE2MD_VISION_CONCURRENCY",
        "notes",
        "vision_concurrency",
        3,
        "int",
        "读图时同时发出的请求数（初筛各轮、读原图各批）；接口限流或本机资源紧张时调小，1 表示逐个发送。",
    ),
    # --------------------------------------------------------------- output
    Setting("COURSE2MD_OUT_DIR", "output", "dir", "course2md-output", "path", "输出目录。"),
    Setting(
        "COURSE2MD_FORMATS",
        "output",
        "formats",
        ["srt", "txt", "md"],
        "list",
        "转写输出格式，可选 srt、vtt、txt、md、json。",
    ),
    Setting(
        "COURSE2MD_DELETE_VIDEO", "output", "delete_video", True, "bool", "网页任务成功写出产物后清理本任务下载的视频。"
    ),
    Setting(
        "COURSE2MD_TERM_START",
        "output",
        "term_start",
        "",
        "str",
        "学期第 1 周起始日（YYYY-MM-DD）；留空时按课程接口或最早课次推断。",
        example="2026-09-07",
    ),
    # ------------------------------------------------------------ resolvers
    Setting(
        "COURSE2MD_RESOLVERS",
        "resolvers",
        "extra",
        [],
        "list",
        "开发时直接加载的解析器（模块:类），不必安装插件。",
        example="course2md_example:ExampleResolver",
    ),
    Setting("COURSE2MD_DISABLED_RESOLVERS", "resolvers", "disabled", [], "list", "按名称停用的解析器。"),
    # -------------------------------------------------------------- runtime
    Setting("COURSE2MD_FFMPEG", "runtime", "ffmpeg", "", "path", "ffmpeg 路径；未安装时用 PyAV 解码音频。"),
    Setting("HF_HOME", "runtime", "hf_home", "", "path", "模型缓存目录，留空使用 Hugging Face 默认位置。"),
    Setting("HF_ENDPOINT", "runtime", "hf_endpoint", "", "url", "模型下载镜像。", example="https://hf-mirror.com"),
    # --------------------------------------------------------------- server
    Setting(
        "COURSE2MD_INSECURE_TLS",
        "server",
        "insecure_tls",
        False,
        "bool",
        "关闭 HTTPS 证书校验，仅在可信网络排查证书问题时使用。",
    ),
    Setting(
        "COURSE2MD_ALLOWED_HOSTS",
        "server",
        "allowed_hosts",
        [],
        "list",
        "网页服务额外接受的主机名；用 --host 0.0.0.0 从局域网访问时填写。",
    ),
    # --------------------------------------------------------------- deploy
    Setting(
        "COURSE2MD_SECRET_KEY",
        "deploy",
        "secret_key",
        "",
        "secret",
        "加密保存来源链接等凭证的密钥，服务器形态必须设置为随机字符串并妥善备份；更换后旧数据无法解密。",
    ),
    Setting(
        "COURSE2MD_DATABASE_URL",
        "deploy",
        "database_url",
        "",
        "secret",
        "数据库地址，例如 postgresql+psycopg://user:pass@db/course2md；留空使用输出目录中的 SQLite。",
    ),
    Setting(
        "COURSE2MD_REGISTRATION",
        "deploy",
        "registration",
        "closed",
        "str",
        "是否开放自助注册。closed 时由管理员在「管理」页创建账号；首个管理员凭服务启动日志中的初始化令牌创建，不受此项影响。",
        choices=("open", "closed"),
    ),
    Setting(
        "COURSE2MD_TRUSTED_PROXIES",
        "deploy",
        "trusted_proxies",
        "127.0.0.1",
        "str",
        "信任其 X-Forwarded-* 头的反向代理地址，逗号分隔；容器部署中 Hub 不对外暴露时可设为 *。",
    ),
    Setting(
        "COURSE2MD_ALLOW_PRIVATE_TRANSFER",
        "deploy",
        "allow_private_transfer",
        False,
        "bool",
        "服务器形态下是否允许普通用户向内网或回环地址的 Hub 互传课程产物（管理员默认允许；云元数据地址始终禁止）。",
    ),
    # --------------------------------------------------------------- worker
    Setting(
        "COURSE2MD_HUB_URL",
        "worker",
        "hub",
        "",
        "url",
        "计算节点连接的 Hub 地址。",
        example="https://course.example.com",
    ),
    Setting(
        "COURSE2MD_WORKER_TOKEN",
        "worker",
        "token",
        "",
        "secret",
        "计算节点令牌，由 Hub 的计算服务页或 course2md worker-token 生成。",
    ),
    Setting("COURSE2MD_WORKER_NAME", "worker", "name", "", "str", "节点名称，默认取主机名。"),
    Setting("COURSE2MD_WORKER_DIR", "worker", "work_dir", "", "path", "节点工作目录，下载的视频与中间文件放在这里。"),
    Setting(
        "COURSE2MD_WORKER_CACHE_DAYS",
        "worker",
        "cache_days",
        "7",
        "int",
        "节点本地视频缓存（media/）自动清理天数，超过该天数未更新的任务缓存将被删除；设为 0 关闭自动清理。",
    ),
)

BY_ENV: dict[str, Setting] = {item.env: item for item in SETTINGS}
TRUE_WORDS = ("1", "true", "yes", "on")
FALSE_WORDS = ("0", "false", "no", "off", "")

_ADAPTERS: dict[str, TypeAdapter] = {
    "int": TypeAdapter(int),
    "float": TypeAdapter(float),
}


def to_env_value(setting: Setting, value: Any) -> str:
    """把配置文件中的值转换成环境变量文本。"""
    if setting.kind == "bool":
        return "1" if value else "0"
    if setting.kind == "list":
        return ",".join(str(item) for item in value) if isinstance(value, (list, tuple)) else str(value)
    return str(value)


def validate_value(setting: Setting, raw: str) -> str | None:
    """检查环境变量文本，合法返回 None，否则返回中文错误说明。"""
    text = raw.strip()
    if setting.kind in _ADAPTERS:
        try:
            _ADAPTERS[setting.kind].validate_python(text)
        except ValidationError:
            return f"{setting.env} 应为{'整数' if setting.kind == 'int' else '数字'}，当前为 {raw!r}"
    elif setting.kind == "bool":
        if text.lower() not in TRUE_WORDS + FALSE_WORDS:
            return f"{setting.env} 应为 1 或 0，当前为 {raw!r}"
    elif setting.kind == "url" and text and not text.startswith(("http://", "https://")):
        return f"{setting.env} 应以 http:// 或 https:// 开头，当前为 {raw!r}"
    if setting.choices and text and text not in setting.choices:
        return f"{setting.env} 可选 {', '.join(setting.choices)}，当前为 {raw!r}"
    return None


def validate_environment(environ: dict[str, str]) -> list[str]:
    """检查当前环境中所有已设置的配置项。"""
    errors = []
    for setting in SETTINGS:
        raw = environ.get(setting.env)
        if raw is not None:
            message = validate_value(setting, raw)
            if message:
                errors.append(message)
    return errors

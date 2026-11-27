"""环境自检：启动前把依赖、模型、显卡、密钥都验一遍。

`course2md doctor` 输出人类可读的检查结果；启动脚本先跑它，避免界面起来了才发现缺东西。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import (
    AsrConfig,
    ConfigError,
    LlmConfig,
    bootstrap_runtime,
    config_path,
    default_out_dir,
    find_model,
    load_config_file,
    load_env_file,
    validate_environment,
)

OK = "✓"
WARN = "!"
BAD = "✗"


@dataclass
class Check:
    """一条检查结果。fatal=True 表示必须修好才能跑。"""

    name: str
    ok: bool
    detail: str
    fatal: bool = False

    @property
    def symbol(self) -> str:
        if self.ok:
            return OK
        return BAD if self.fatal else WARN


def _check_config_files() -> list[Check]:
    """Python 版本、.env、配置文件与环境变量校验。"""
    checks: list[Check] = []
    checks.append(Check("Python", sys.version_info >= (3, 10), f"{sys.version.split()[0]}（需要 >=3.10）", fatal=True))

    env_file = load_env_file()
    checks.append(
        Check(".env", env_file is not None, str(env_file) if env_file else "未找到（可选，可用环境变量代替）")
    )

    try:
        config_file = load_config_file()
    except ConfigError as exc:
        checks.append(Check("配置文件", False, str(exc), fatal=True))
    else:
        checks.append(
            Check("配置文件", True, str(config_file) if config_file else f"未创建（可选，位置 {config_path()}）")
        )
    for problem in validate_environment(dict(os.environ)):
        checks.append(Check("配置项", False, problem, fatal=True))
    return checks


def _check_asr_runtime() -> list[Check]:
    """CUDA 运行库、faster-whisper、转写设备、模型文件与 GPU。"""
    checks: list[Check] = []
    cuda_dirs = bootstrap_runtime()
    checks.append(
        Check(
            "CUDA 运行库",
            True,
            "、".join(str(path) for path in cuda_dirs)
            if cuda_dirs
            else "未通过 pip 安装（GPU 转写需要 course2md[cuda] 或系统 CUDA）",
        )
    )

    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:
        checks.append(Check("faster-whisper", False, f"未安装：{exc}", fatal=True))
    else:
        checks.append(Check("faster-whisper", True, "已安装"))

    asr = AsrConfig.from_env()
    checks.append(Check("转写设备", True, f"{asr.device} / {asr.compute_type} / 模型 {asr.model}"))

    model_path = find_model(asr.model)
    is_dir = Path(model_path).is_dir()
    checks.append(Check("模型文件", is_dir, model_path if is_dir else f"本地未找到（首次运行会联网下载 {asr.model}）"))

    if asr.device == "cuda":
        try:
            import ctranslate2

            count = ctranslate2.get_cuda_device_count()
        except Exception as exc:  # noqa: BLE001
            checks.append(Check("GPU", False, f"无法检测 CUDA：{exc}（可改用 --cpu）"))
        else:
            checks.append(Check("GPU", count > 0, f"检测到 {count} 张 CUDA 设备"))
    else:
        checks.append(Check("GPU", True, "使用 CPU，跳过 CUDA 检查"))
    return checks


def _check_llm() -> Check:
    llm = LlmConfig.from_env()
    key = llm.resolved_key()
    return Check("大模型", bool(key), f"{llm.base_url} / {llm.model}" + ("" if key else "（未配置 Key，只能转写）"))


def _check_output_dir() -> list[Check]:
    """输出目录可写性；可写且在 Windows 上时再检查 Agent 沙箱授权。"""
    checks: list[Check] = []
    out_dir = Path(default_out_dir())
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        probe = out_dir / ".course2md-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable, detail = True, str(out_dir.resolve())
    except OSError as exc:
        writable, detail = False, f"{out_dir} 不可写：{exc}"
    checks.append(Check("输出目录", writable, detail, fatal=True))

    if writable and os.name == "nt":
        from .util.winacl import ACL_HINT, acl_grant_problem

        acl_problem = acl_grant_problem(out_dir)
        checks.append(
            Check(
                "Agent 沙箱授权",
                acl_problem is None,
                "当前进程可为工作目录授权" if acl_problem is None else f"{acl_problem}。{ACL_HINT}",
            )
        )
    return checks


def _check_resolvers() -> list[Check]:
    from .resolvers import available, load_errors

    checks: list[Check] = []
    resolvers = available()
    names = "、".join(
        f"{info.label}（{info.name}"
        + (f"，{info.origin} {info.version}".rstrip() if info.origin != "builtin" else "")
        + "）"
        for info in resolvers
    )
    checks.append(Check("来源解析器", True, names))
    for error in load_errors():
        checks.append(Check("解析器插件", False, error))
    return checks


def run_checks() -> list[Check]:
    """执行全部检查，返回结果列表。"""
    checks: list[Check] = []
    checks.extend(_check_config_files())
    checks.extend(_check_asr_runtime())
    checks.append(_check_llm())
    checks.extend(_check_output_dir())
    checks.extend(_check_resolvers())
    return checks


def report(checks: list[Check]) -> bool:
    """打印检查结果，返回是否全部致命项通过。"""
    width = max(len(c.name) for c in checks) + 2
    for check in checks:
        print(f"  {check.symbol} {check.name.ljust(width)}{check.detail}")
    fatal = [c for c in checks if not c.ok and c.fatal]
    print()
    if fatal:
        print(f"有 {len(fatal)} 项必须修复：{', '.join(c.name for c in fatal)}")
        return False
    warns = [c for c in checks if not c.ok]
    print("环境就绪" + (f"（{len(warns)} 项提示可忽略）" if warns else ""))
    return True


def main() -> int:
    print(f"course2md 环境自检 · 工作目录 {Path.cwd()}\n")
    return 0 if report(run_checks()) else 1

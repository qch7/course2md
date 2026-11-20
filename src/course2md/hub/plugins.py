"""本地应用的来源解析插件安装与卸载。"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path
from urllib.parse import unquote, urlparse

from ..core.accounts import LOCAL_USER, User
from ..resolvers import available, load_errors
from ..resolvers.registry import ENTRY_POINT_GROUP
from ..util.proc import NO_WINDOW
from .service import Forbidden, NotFound, server_mode

MAX_UPLOAD_BYTES = 32 * 1024 * 1024
ARCHIVE_SUFFIXES = (".whl", ".zip", ".tar.gz")
_operation_lock = threading.Lock()
_restart_required = False


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _distributions(path: Path | None = None) -> list[metadata.Distribution]:
    distributions = metadata.distributions(path=[str(path)]) if path is not None else metadata.distributions()
    return [dist for dist in distributions if any(point.group == ENTRY_POINT_GROUP for point in dist.entry_points)]


def list_plugins(manager, user: User = LOCAL_USER) -> dict:
    can_manage = not server_mode(manager) and user.is_admin
    infos = available()
    plugins = []
    for info in infos:
        if info.origin in ("builtin", "env"):
            plugins.append(
                {
                    "name": info.name,
                    "label": info.label,
                    "hint": info.source_hint,
                    "version": info.version,
                    "origin": info.origin,
                    "resolvers": [info.name],
                    "removable": False,
                }
            )
    seen = set()
    for dist in _distributions():
        name = dist.name
        key = _normalized(name)
        if not name or key in seen or key == "course2md":
            continue
        seen.add(key)
        matches = [info for info in infos if _normalized(info.origin) == key]
        plugins.append(
            {
                "name": name,
                "label": "、".join(info.label for info in matches) or name,
                "hint": "；".join(info.source_hint for info in matches if info.source_hint),
                "version": dist.version,
                "origin": "package",
                "resolvers": [point.name for point in dist.entry_points if point.group == ENTRY_POINT_GROUP],
                "removable": can_manage,
            }
        )
    return {
        "plugins": sorted(plugins, key=lambda item: (item["origin"] == "builtin", item["name"])),
        "errors": load_errors(),
        "can_manage": can_manage,
        "restart_required": _restart_required,
    }


def check_access(manager, user: User) -> None:
    if server_mode(manager) or not user.is_admin:
        raise Forbidden("网页插件管理仅供本地应用使用；服务器插件请通过部署配置管理")


def ensure_ready() -> None:
    if _operation_lock.locked():
        raise ValueError("正在修改插件，请完成后再操作")
    if _restart_required:
        raise ValueError("插件已变更，请重启 course2md 后再处理课程")


@contextmanager
def _mutation(manager, user: User) -> Iterator[None]:
    check_access(manager, user)
    if not _operation_lock.acquire(blocking=False):
        raise ValueError("已有插件操作正在进行，请稍后再试")
    try:
        with manager._lock:
            if any(job.status in ("queued", "running") or job.pending_messages for job in manager.jobs.values()):
                raise ValueError("课程仍在处理，请等待任务结束后再修改插件")
        yield
    finally:
        _operation_lock.release()


def _source(source: str, kind: str) -> str:
    source = source.strip()
    if not source:
        raise ValueError("请填写插件来源")
    if kind == "github":
        address = urlparse(source.removeprefix("git+"))
        parts = address.path.strip("/").split("/")
        if (
            address.scheme != "https"
            or address.netloc.lower() != "github.com"
            or address.query
            or address.fragment
            or len(parts) not in (2, 4)
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", "..") for part in parts[:2])
            or (len(parts) == 4 and parts[2] != "tree")
        ):
            raise ValueError("请填写 GitHub 仓库的 HTTPS 链接，可包含 /tree/分支或版本")
        repository = parts[1].removesuffix(".git")
        revision = ""
        if len(parts) == 4:
            reference = unquote(parts[3])
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", reference):
                raise ValueError("GitHub 分支或版本格式无效")
            revision = f"@{reference}"
        return f"git+https://github.com/{parts[0]}/{repository}.git{revision}"
    if kind != "local":
        raise ValueError("安装来源必须是 github 或 local")
    path = Path(source).expanduser().resolve()
    if path.is_dir() and any((path / name).is_file() for name in ("pyproject.toml", "setup.py")):
        return str(path)
    if path.is_file() and path.name.lower().endswith(ARCHIVE_SUFFIXES):
        return str(path)
    raise ValueError("请选择插件项目目录，或 .whl、.zip、.tar.gz 安装包")


def _run_package(arguments: list[str], cache_dir: Path) -> None:
    uv = shutil.which("uv")
    if uv:
        command = [uv, "pip", arguments[0], "--python", sys.executable, *arguments[1:]]
    elif importlib.util.find_spec("pip") is not None:
        command = [sys.executable, "-m", "pip", *arguments]
        if arguments[0] == "uninstall":
            command.append("-y")
    else:
        raise ValueError("当前环境找不到 uv 或 pip，请先安装 uv 后重试")
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "PIP_NO_INPUT": "1",
                "UV_NO_PROGRESS": "1",
                "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR") or str(cache_dir),
                "PIP_CACHE_DIR": os.environ.get("PIP_CACHE_DIR") or str(cache_dir),
            },
            creationflags=NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("插件操作超时，请检查网络或安装来源后重试") from exc
    except OSError as exc:
        raise ValueError(f"无法启动插件安装工具：{exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "安装工具未返回错误说明").strip()[-3000:]
        raise ValueError(f"插件操作失败：{detail}")


def install_plugin(manager, source: str, kind: str, user: User = LOCAL_USER) -> dict:
    global _restart_required
    with _mutation(manager, user):
        target = _source(source, kind)
        with tempfile.TemporaryDirectory(prefix="course2md-plugin-") as temporary:
            root = Path(temporary)
            staged = root / "check"
            _run_package(["install", "--no-deps", "--target", str(staged), target], root / "cache")
            distributions = _distributions(staged)
            if len(distributions) != 1 or _normalized(distributions[0].name) == "course2md":
                raise ValueError("安装来源不是有效的 course2md 来源解析插件：缺少 course2md.resolvers 入口点")
            constraint = root / "constraints.txt"
            constraint.write_text(f"course2md=={metadata.version('course2md')}\n", encoding="utf-8")
            _restart_required = True
            _run_package(["install", "--upgrade", "--constraint", str(constraint), target], root / "cache")
    return {**list_plugins(manager, user), "message": "插件已安装，请重启 course2md 后生效"}


def validate_filename(filename: str) -> str:
    if (
        not filename
        or any(character in filename for character in '\\/:<>"|?*')
        or len(filename) > 200
        or any(ord(character) < 32 for character in filename)
        or not filename.lower().endswith(ARCHIVE_SUFFIXES)
    ):
        raise ValueError("请选择 .whl、.zip 或 .tar.gz 插件安装包")
    return filename


def import_plugin(manager, filename: str, content: bytes, user: User = LOCAL_USER) -> dict:
    check_access(manager, user)
    filename = validate_filename(filename)
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("插件安装包不能为空，且不能超过 32 MB")
    with tempfile.TemporaryDirectory(prefix="course2md-upload-") as temporary:
        path = Path(temporary) / filename
        path.write_bytes(content)
        return install_plugin(manager, str(path), "local", user)


def delete_plugin(manager, name: str, user: User = LOCAL_USER) -> dict:
    global _restart_required
    with _mutation(manager, user):
        name = next(
            (
                dist.name
                for dist in _distributions()
                if _normalized(dist.name) == _normalized(name) and _normalized(name) != "course2md"
            ),
            "",
        )
        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            raise NotFound("插件不存在或不能删除；内置解析器和开发加载的插件不能卸载")
        _restart_required = True
        with tempfile.TemporaryDirectory(prefix="course2md-plugin-") as temporary:
            _run_package(["uninstall", name], Path(temporary) / "cache")
    return {**list_plugins(manager, user), "message": "插件已删除，请重启 course2md 后生效"}

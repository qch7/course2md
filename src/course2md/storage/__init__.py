"""产物存储：按内容的 SHA-256 保存字节，相同内容只存一份。

目前实现为本地文件系统（:class:`LocalFileStore`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Protocol

DIGEST_LENGTH = 64


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def canonical_json(value: Any) -> bytes:
    """确定的 JSON 编码，同一内容得到同一摘要。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _check_digest(digest: str) -> str:
    if len(digest) != DIGEST_LENGTH or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"无效的内容摘要：{digest!r}")
    return digest


class ArtifactStore(Protocol):
    def put_bytes(self, data: bytes) -> str: ...
    def get_bytes(self, digest: str) -> bytes: ...
    def exists(self, digest: str) -> bool: ...
    def copy_to(self, digest: str, target: Path) -> Path: ...


class StoreMixin:
    def get_json(self, digest: str) -> Any:
        return json.loads(self.get_bytes(digest))  # type: ignore[attr-defined]


class LocalFileStore(StoreMixin):
    """objects/ab/cdef… 的目录布局，写入先落临时文件再原子改名。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def path(self, digest: str) -> Path:
        _check_digest(digest)
        return self.root / digest[:2] / digest[2:]

    def exists(self, digest: str) -> bool:
        return self.path(digest).is_file()

    def touch(self, digest: str) -> bool:
        """刷新对象文件的修改时间，避免正在复用或上传中的对象被 GC 误删。"""
        target = self.path(digest)
        if not target.is_file():
            return False
        try:
            os.utime(target, None)
        except OSError:
            return False
        return True

    def iter_digests(self) -> list[tuple[str, float]]:
        """列举存储中全部有效对象的 (摘要, 修改时间)。"""
        if not self.root.is_dir():
            return []
        found: list[tuple[str, float]] = []
        for bucket in self.root.iterdir():
            if not bucket.is_dir() or len(bucket.name) != 2:
                continue
            for entry in bucket.iterdir():
                if not entry.is_file() or entry.name.startswith("."):
                    continue
                digest = bucket.name + entry.name
                try:
                    _check_digest(digest)
                    mtime = entry.stat().st_mtime
                except (ValueError, OSError):
                    continue
                found.append((digest, mtime))
        return found

    def delete(self, digest: str) -> bool:
        """删除对象文件；若前缀目录变空则顺带清理。"""
        target = self.path(digest)
        if not target.is_file():
            return False
        try:
            target.unlink()
        except OSError:
            return False
        try:
            target.parent.rmdir()
        except OSError:
            pass
        return True

    def put_bytes(self, data: bytes) -> str:
        digest = digest_bytes(data)
        target = self.path(digest)
        if not target.is_file():
            self._commit(target, lambda handle: handle.write(data))
        else:
            self.touch(digest)
        return digest

    def put_stream(self, stream: BinaryIO) -> str:
        """边读边算摘要，适合上传。"""
        self.root.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        handle, temp = tempfile.mkstemp(dir=self.root, prefix=".upload-")
        try:
            with os.fdopen(handle, "wb") as output:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
                    output.write(block)
            digest = hasher.hexdigest()
            target = self.path(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file():
                os.unlink(temp)
                self.touch(digest)
            else:
                os.replace(temp, target)
            return digest
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def get_bytes(self, digest: str) -> bytes:
        try:
            return self.path(digest).read_bytes()
        except FileNotFoundError:
            raise KeyError(digest) from None

    def open(self, digest: str) -> BinaryIO:
        try:
            return self.path(digest).open("rb")
        except FileNotFoundError:
            raise KeyError(digest) from None

    def copy_to(self, digest: str, target: Path) -> Path:
        source = self.path(digest)
        if not source.is_file():
            raise KeyError(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.part")
        shutil.copyfile(source, temp)
        os.replace(temp, target)
        return target

    def _commit(self, target: Path, write) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(handle, "wb") as output:
                write(output)
            os.replace(temp, target)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise


__all__ = ["ArtifactStore", "LocalFileStore", "canonical_json", "digest_bytes", "file_digest"]

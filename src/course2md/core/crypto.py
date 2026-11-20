"""来源链接等敏感字段的加密存储。

设置了 ``COURSE2MD_SECRET_KEY`` 时用 Fernet（AES-128-CBC + HMAC）加密，密文带 ``enc:v1:`` 前缀；
未设置时（本地模式）原样保存。解密只在派发下载任务、刷新课次等确实需要时进行。
需要 ``course2md[server]`` 中的 cryptography。
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

PREFIX = "enc:v1:"
KEY_ENV = "COURSE2MD_SECRET_KEY"


class CryptoError(RuntimeError):
    pass


class FieldCipher:
    def __init__(self, secret: str | None) -> None:
        self._fernet = None
        if secret:
            try:
                from cryptography.fernet import Fernet
            except ImportError:
                raise CryptoError(
                    '设置了 COURSE2MD_SECRET_KEY，但未安装 cryptography（pip install "course2md[server]"）'
                ) from None
            key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
            self._fernet = Fernet(key)

    @classmethod
    def from_env(cls) -> FieldCipher:
        return cls(os.environ.get(KEY_ENV, "").strip() or None)

    def encrypt(self, value: str) -> str:
        if self._fernet is None or not value or value.startswith(PREFIX):
            return value
        return PREFIX + self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        if not isinstance(value, str) or not value.startswith(PREFIX):
            return value
        if self._fernet is None:
            raise CryptoError("数据已加密，但没有设置 COURSE2MD_SECRET_KEY")
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(value[len(PREFIX) :].encode("ascii")).decode("utf-8")
        except InvalidToken:
            raise CryptoError("无法解密：COURSE2MD_SECRET_KEY 与加密时不一致") from None

    def map_fields(self, data: dict[str, Any], paths: tuple[tuple[str, ...], ...], encrypt: bool) -> dict[str, Any]:
        """按路径加密或解密嵌套字典中的字符串字段，返回副本。"""
        if self._fernet is None and encrypt:
            return data
        result = _copy(data)
        for path in paths:
            node = result
            for key in path[:-1]:
                node = node.get(key) if isinstance(node, dict) else None
                if node is None:
                    break
            if isinstance(node, dict) and isinstance(node.get(path[-1]), str):
                node[path[-1]] = self.encrypt(node[path[-1]]) if encrypt else self.decrypt(node[path[-1]])
        return result


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


# 课程、任务与阶段任务中含凭证的字段
COURSE_FIELDS = (("source",),)
JOB_FIELDS = (("source",), ("options", "source"))
TASK_FIELDS = (("options", "source"),)

"""账号与登录会话。

- 口令用 scrypt 加盐哈希（标准库，无额外依赖）。
- 首个管理员不再由“第一个到达的人”抢占：启动时若还没有账号，会生成一次性初始化令牌打印到服务日志，
  只有持有该令牌的人才能创建首个管理员（做法同 Jenkins、Jupyter）。令牌只存在于进程内存，创建成功即失效。
- 之后是否开放自助注册由 ``COURSE2MD_REGISTRATION`` 决定（open / closed），其余账号由管理员创建与管理。
- 登录得到一个随机令牌，网页放在 HttpOnly Cookie 中，其它客户端用 ``Authorization: Bearer``。数据库只存令牌摘要。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update

from .db import Database
from .models import SessionRow, UserRow

SESSION_SECONDS = 30 * 24 * 3600
USERNAME = re.compile(r"[A-Za-z0-9_.\-一-鿿]{2,32}")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


@dataclass(frozen=True)
class User:
    id: str
    username: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def public(self) -> dict:
        return {"id": self.id, "username": self.username, "role": self.role}


# 本地模式（不需要登录）下的唯一用户，拥有全部权限。
LOCAL_USER = User(id="local", username="本机", role="admin")


class AccountError(ValueError):
    """注册、登录或账号管理失败，消息可直接展示给用户。"""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), dklen=32, **_SCRYPT)
    return hmac.compare_digest(actual, base64.b64decode(digest))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user(row: UserRow) -> User:
    return User(id=row.id, username=row.username, role=row.role)


class Accounts:
    def __init__(self, db: Database, registration: str = "closed") -> None:
        self.db = db
        self.registration = registration
        self._setup_token: str | None = None
        # 创建账号时“检查是否已有用户”与“写入”必须是一个整体，否则两个并发请求都能成为首个管理员
        self._create_lock = threading.Lock()

    # ------------------------------------------------------------ 初始化
    def count(self) -> int:
        with self.db.session() as session:
            return int(session.scalar(select(func.count()).select_from(UserRow)) or 0)

    def begin_setup(self) -> str | None:
        """还没有任何账号时生成初始化令牌并返回，由调用方打印到服务日志；已有账号则返回 None。

        重复调用返回同一个令牌，避免日志里出现多个互相矛盾的值。
        """
        with self._create_lock:
            if self.count() > 0:
                self._setup_token = None
            elif self._setup_token is None:
                self._setup_token = f"c2s_{secrets.token_urlsafe(24)}"
            return self._setup_token

    @property
    def needs_setup(self) -> bool:
        return self.count() == 0

    def setup(self, username: str, password: str, token: str) -> User:
        """用初始化令牌创建首个管理员。令牌错误时不透露是否已有账号之外的信息。"""
        username = self._check_credentials(username, password)
        with self._create_lock:
            if self.count() > 0:
                self._setup_token = None
                raise AccountError("已完成初始化，请直接登录")
            expected = self._setup_token
            if expected is None:
                raise AccountError("服务没有生成初始化令牌，请重启服务后从启动日志中获取")
            if not hmac.compare_digest(token.strip().encode("utf-8"), expected.encode("utf-8")):
                raise AccountError("初始化令牌不正确，请使用服务启动日志中打印的令牌")
            user = self._create(username, password, "admin")
            self._setup_token = None
            return user

    # ------------------------------------------------------------ 用户
    @staticmethod
    def _check_credentials(username: str, password: str) -> str:
        username = username.strip()
        if not USERNAME.fullmatch(username):
            raise AccountError("用户名为 2–32 个字母、数字、汉字或 . _ -")
        if len(password) < 8:
            raise AccountError("口令至少 8 位")
        return username

    def _create(self, username: str, password: str, role: str) -> User:
        with self.db.session() as session:
            exists = session.scalars(select(UserRow).where(func.lower(UserRow.username) == username.lower())).first()
            if exists is not None:
                raise AccountError("用户名已被使用")
            row = UserRow(
                id=uuid.uuid4().hex[:16],
                username=username,
                password_hash=hash_password(password),
                role=role,
                disabled=False,
                created_at=time.time(),
            )
            session.add(row)
            return _user(row)

    def register(self, username: str, password: str, by: User | None = None, role: str | None = None) -> User:
        """by 为执行创建的管理员；为空表示自助注册。首个管理员只能经 :meth:`setup` 创建。"""
        username = self._check_credentials(username, password)
        with self._create_lock:
            if self.count() == 0:
                raise AccountError("服务尚未初始化，请使用启动日志中的初始化令牌创建管理员")
            if not (by and by.is_admin) and self.registration != "open":
                raise AccountError("本站未开放注册，请联系管理员创建账号")
            role = role if by and by.is_admin and role in ("admin", "user") else "user"
            return self._create(username, password, role)

    def users(self) -> list[dict]:
        with self.db.session() as session:
            rows = session.scalars(select(UserRow).order_by(UserRow.created_at))
            return [
                {
                    "id": row.id,
                    "username": row.username,
                    "role": row.role,
                    "disabled": row.disabled,
                    "created_at": row.created_at,
                }
                for row in rows
            ]

    def set_disabled(self, user_id: str, disabled: bool, by: User) -> None:
        if not by.is_admin:
            raise AccountError("只有管理员可以停用账号")
        if user_id == by.id:
            raise AccountError("不能停用自己")
        with self.db.session() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                raise AccountError("用户不存在")
            row.disabled = disabled
            if disabled:
                for item in session.scalars(select(SessionRow).where(SessionRow.user_id == user_id)):
                    session.delete(item)

    def set_role(self, user_id: str, role: str, by: User) -> None:
        if not by.is_admin:
            raise AccountError("只有管理员可以修改角色")
        if role not in ("admin", "user"):
            raise AccountError("角色只能是 admin 或 user")
        # 操作者自己不能降级，保证任何时候至少留有一个可用的管理员
        if user_id == by.id:
            raise AccountError("不能修改自己的角色")
        with self.db.session() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                raise AccountError("用户不存在")
            row.role = role

    def reset_password(self, user_id: str, new: str, by: User) -> None:
        """管理员重置他人口令；同时注销该用户全部会话与 API 令牌，旧口令的持有者随即失效。"""
        if not by.is_admin:
            raise AccountError("只有管理员可以重置口令")
        if user_id == by.id:
            raise AccountError("请在“修改口令”中修改自己的口令")
        if len(new) < 8:
            raise AccountError("口令至少 8 位")
        with self.db.session() as session:
            row = session.get(UserRow, user_id)
            if row is None:
                raise AccountError("用户不存在")
            row.password_hash = hash_password(new)
            for item in session.scalars(select(SessionRow).where(SessionRow.user_id == user_id)):
                session.delete(item)

    def change_password(self, user: User, old: str, new: str) -> None:
        if len(new) < 8:
            raise AccountError("口令至少 8 位")
        with self.db.session() as session:
            row = session.get(UserRow, user.id)
            if row is None or not verify_password(old, row.password_hash):
                raise AccountError("原口令不正确")
            row.password_hash = hash_password(new)

    # ------------------------------------------------------------ 会话
    def login(self, username: str, password: str) -> tuple[User, str]:
        with self.db.session() as session:
            row = session.scalars(
                select(UserRow).where(func.lower(UserRow.username) == username.strip().lower())
            ).first()
            valid = row is not None and verify_password(password, row.password_hash)
            if not valid or row is None or row.disabled:
                raise AccountError("用户名或口令不正确")
            user = _user(row)
        return user, self.issue(user, "session", expires=SESSION_SECONDS)

    def issue(self, user: User, kind: str = "api", name: str = "", expires: float | None = None) -> str:
        token = f"c2u_{secrets.token_urlsafe(32)}"
        now = time.time()
        with self.db.session() as session:
            session.add(
                SessionRow(
                    id=uuid.uuid4().hex,
                    user_id=user.id,
                    token_hash=_token_hash(token),
                    kind=kind,
                    name=name,
                    created_at=now,
                    expires_at=now + expires if expires else None,
                    last_used=now,
                )
            )
        return token

    def resolve(self, token: str) -> User | None:
        if not token:
            return None
        now = time.time()
        with self.db.session() as session:
            item = session.scalars(select(SessionRow).where(SessionRow.token_hash == _token_hash(token))).first()
            if item is None or (item.expires_at is not None and item.expires_at < now):
                return None
            row = session.get(UserRow, item.user_id)
            if row is None or row.disabled:
                return None
            if now - item.last_used > 300:
                session.execute(update(SessionRow).where(SessionRow.id == item.id).values(last_used=now))
            return _user(row)

    def logout(self, token: str) -> None:
        with self.db.session() as session:
            for item in session.scalars(select(SessionRow).where(SessionRow.token_hash == _token_hash(token))):
                session.delete(item)

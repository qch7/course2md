"""登录与用户接口：/api/v1/auth/*、/api/v1/users。

本地模式（mode=local）不需要登录，所有请求都以本机用户身份执行；服务器模式（mode=server）
要求登录 Cookie 或 ``Authorization: Bearer`` 令牌。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..core.accounts import LOCAL_USER, AccountError, Accounts, User
from . import responses as r

COOKIE = "course2md_session"
router = APIRouter(tags=["账号"])

# 同一来源地址 10 分钟内最多失败 10 次，防止暴力猜测口令
FAILURE_WINDOW, FAILURE_LIMIT = 600.0, 10
_failures: dict[str, deque[float]] = defaultdict(deque)
_failures_lock = threading.Lock()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _throttle(request: Request) -> None:
    now = time.monotonic()
    with _failures_lock:
        attempts = _failures[_client(request)]
        while attempts and now - attempts[0] > FAILURE_WINDOW:
            attempts.popleft()
        if len(attempts) >= FAILURE_LIMIT:
            raise HTTPException(status_code=429, detail="尝试次数过多，请 10 分钟后再试")


def _record_failure(request: Request) -> None:
    with _failures_lock:
        _failures[_client(request)].append(time.monotonic())


class LoginRequired(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=401, detail="请先登录")


def _accounts(request: Request) -> Accounts | None:
    return getattr(request.app.state, "accounts", None)


def _token(request: Request) -> str:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return request.cookies.get(COOKIE, "")


def current_user(request: Request) -> User:
    accounts = _accounts(request)
    if accounts is None:  # 本地模式
        return LOCAL_USER
    user = accounts.resolve(_token(request))
    if user is None:
        raise LoginRequired()
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _require_accounts(request: Request) -> Accounts:
    accounts = _accounts(request)
    if accounts is None:
        raise HTTPException(status_code=404, detail="本地模式不需要登录")
    return accounts


class Credentials(BaseModel):
    username: str = Field("", max_length=64)
    password: str = Field("", max_length=256)


class NewUser(Credentials):
    role: str = "user"


class PasswordChange(BaseModel):
    old_password: str = Field("", max_length=256)
    new_password: str = Field("", max_length=256)


class UserUpdate(BaseModel):
    disabled: bool


class TokenRequest(BaseModel):
    name: str = Field("", max_length=100)


def _set_cookie(request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https"
    response.set_cookie(COOKIE, token, max_age=30 * 24 * 3600, httponly=True, secure=secure, samesite="lax", path="/")


@router.get("/auth/status", summary="登录要求与注册是否开放", response_model=r.AuthStatus)
def status(request: Request) -> dict[str, Any]:
    accounts = _accounts(request)
    if accounts is None:
        return {"mode": "local", "registration": "closed", "has_users": True}
    return {"mode": "server", "registration": accounts.registration, "has_users": accounts.count() > 0}


@router.get("/auth/me", summary="当前用户", response_model=r.UserResponse)
def me(user: User = Depends(current_user)) -> dict[str, Any]:
    return {"user": user.public()}


@router.post("/auth/register", summary="注册；第一个用户成为管理员", response_model=r.UserResponse)
def register(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
    accounts = _require_accounts(request)
    user = accounts.register(body.username, body.password)
    _, token = accounts.login(body.username, body.password)
    _set_cookie(request, response, token)
    return {"user": user.public()}


@router.post("/auth/login", summary="登录，设置会话 Cookie 并返回令牌", response_model=r.LoginResponse)
def login(body: Credentials, request: Request, response: Response) -> dict[str, Any]:
    accounts = _require_accounts(request)
    _throttle(request)
    try:
        user, token = accounts.login(body.username, body.password)
    except AccountError:
        _record_failure(request)
        raise
    _set_cookie(request, response, token)
    return {"user": user.public(), "token": token}


@router.post("/auth/logout", summary="退出登录", response_model=r.Ok)
def logout(request: Request, response: Response) -> dict[str, Any]:
    accounts = _accounts(request)
    token = _token(request)
    if accounts is not None and token:
        accounts.logout(token)
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/password", summary="修改口令", response_model=r.Ok)
def change_password(body: PasswordChange, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    _require_accounts(request).change_password(user, body.old_password, body.new_password)
    return {"ok": True}


@router.post(
    "/auth/tokens", summary="生成 API 令牌（供脚本或其它客户端使用），只在本次返回", response_model=r.TokenResponse
)
def create_token(body: TokenRequest, request: Request, user: User = Depends(current_user)) -> dict[str, Any]:
    return {"token": _require_accounts(request).issue(user, "api", body.name or "API 令牌")}


@router.get("/users", summary="全部用户（管理员）", response_model=r.UsersResponse)
def list_users(request: Request, _admin: User = Depends(admin_user)) -> dict[str, Any]:
    return {"users": _require_accounts(request).users()}


@router.post("/users", summary="创建用户（管理员）", response_model=r.UserResponse)
def create_user(body: NewUser, request: Request, admin: User = Depends(admin_user)) -> dict[str, Any]:
    user = _require_accounts(request).register(body.username, body.password, by=admin, role=body.role)
    return {"user": user.public()}


@router.patch("/users/{user_id}", summary="停用或启用用户（管理员）", response_model=r.Ok)
def update_user(user_id: str, body: UserUpdate, request: Request, admin: User = Depends(admin_user)) -> dict[str, Any]:
    _require_accounts(request).set_disabled(user_id, body.disabled, admin)
    return {"ok": True}


__all__ = ["COOKIE", "AccountError", "admin_user", "current_user", "router"]

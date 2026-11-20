"""数据库表。

courses / jobs 以整行 JSON 保存课程与任务记录；
tasks / task_events / artifacts / workers 支撑阶段级任务队列与产物记录。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class CourseRow(Base):
    __tablename__ = "courses"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)


class JobRow(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    out_dir: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    data: Mapped[str] = mapped_column(Text, nullable=False)


class TaskRow(Base):
    """一个阶段的一次执行。status：pending → leased → done / failed / cancelled。"""

    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # 任务所属用户
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), index=True, default="pending")
    requires: Mapped[list[str]] = mapped_column(JSON, default=list)
    affinity: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 必须由该节点执行
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # 选项与输入产物
    cache_key: Mapped[str] = mapped_column(String(64), index=True, default="")
    result: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 输出产物摘要
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    error_type: Mapped[str] = mapped_column(String(64), default="")
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    progress_stage: Mapped[str] = mapped_column(String(32), default="")
    progress_fraction: Mapped[float] = mapped_column(Float, default=0.0)
    progress_message: Mapped[str] = mapped_column(Text, default="")
    progress_sub: Mapped[float | None] = mapped_column(Float, nullable=True)
    progress_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)
    started_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class TaskEventRow(Base):
    """阶段运行输出（Agent 事件等），按自增 id 增量读取。"""

    __tablename__ = "task_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON)


class ArtifactRow(Base):
    """产物的一次生成记录。内容按 digest 存在产物存储中，相同内容可有多条生成记录。"""

    __tablename__ = "artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    worker_id: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float)


class WorkerRow(Base):
    """计算节点。kind：builtin（Hub 进程内）或 remote。"""

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="builtin")
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    version: Mapped[str] = mapped_column(String(32), default="")
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_seen: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[float] = mapped_column(Float)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class UserRow(Base):
    """账号。role：admin 或 user。口令以 scrypt 加盐哈希保存。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="user")
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[float] = mapped_column(Float)


class SessionRow(Base):
    """登录会话或 API 令牌，只保存令牌的 SHA-256。"""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default="session")  # session 或 api
    name: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float)
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_used: Mapped[float] = mapped_column(Float, default=0.0)


class NoteVersionRow(Base):
    """笔记版本：两版 Markdown 的内容摘要（内容在产物存储中）。"""

    __tablename__ = "note_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    detailed: Mapped[str] = mapped_column(String(64))
    brief: Mapped[str] = mapped_column(String(64), default="")
    label: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float)

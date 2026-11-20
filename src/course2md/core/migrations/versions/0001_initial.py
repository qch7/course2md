"""初始结构：课程、任务、阶段任务、产物、计算节点、账号、笔记版本

Revision ID: c2m1
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2m1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "courses",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("data", sa.Text(), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("out_dir", sa.Text(), nullable=False, unique=True),
        sa.Column("data", sa.Text(), nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(64), nullable=False, index=True),
        sa.Column("owner_id", sa.String(64), nullable=True, index=True),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("requires", sa.JSON(), nullable=False),
        sa.Column("affinity", sa.String(64), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("cache_key", sa.String(64), nullable=False, index=True),
        sa.Column("result", sa.String(64), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("error_type", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=True, index=True),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("progress_stage", sa.String(32), nullable=False),
        sa.Column("progress_fraction", sa.Float(), nullable=False),
        sa.Column("progress_message", sa.Text(), nullable=False),
        sa.Column("progress_sub", sa.Float(), nullable=True),
        sa.Column("progress_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
    )
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String(32), nullable=False, index=True),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("digest", sa.String(64), nullable=False, index=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False, index=True),
        sa.Column("task_id", sa.String(32), nullable=False, index=True),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "workers",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(128), nullable=True, unique=True),
        sa.Column("owner_id", sa.String(64), nullable=True),
        sa.Column("last_seen", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("token_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=True),
        sa.Column("last_used", sa.Float(), nullable=False),
    )
    op.create_table(
        "note_versions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(64), nullable=False, index=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("detailed", sa.String(64), nullable=False),
        sa.Column("brief", sa.String(64), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("job_id", "version", name="uq_note_versions_job_version"),
    )


def downgrade() -> None:
    for table in (
        "note_versions",
        "sessions",
        "users",
        "workers",
        "artifacts",
        "task_events",
        "tasks",
        "jobs",
        "courses",
    ):
        op.drop_table(table)

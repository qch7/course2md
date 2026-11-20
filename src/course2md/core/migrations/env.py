"""Alembic 环境：连接由 course2md.core.db.upgrade 传入。"""

from __future__ import annotations

from alembic import context

from course2md.core.models import Base

connection = context.config.attributes["connection"]
context.configure(
    connection=connection, target_metadata=Base.metadata, render_as_batch=connection.dialect.name == "sqlite"
)
with context.begin_transaction():
    context.run_migrations()

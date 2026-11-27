"""数据库连接与迁移。本地默认 SQLite（WAL 模式），服务器通过 COURSE2MD_DATABASE_URL 使用 PostgreSQL。"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

MIGRATIONS = Path(__file__).parent / "migrations"
_migrate_lock = threading.Lock()


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


class Database:
    """一个数据库的引擎与会话工厂。首次打开时自动升级到最新结构。"""

    def __init__(self, url: str, migrate: bool = True) -> None:
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            path = url.removeprefix("sqlite:///")
            if path and path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.engine: Engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})
            event.listen(self.engine, "connect", _sqlite_pragmas)
        else:
            self.engine = create_engine(url, pool_pre_ping=True)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        if migrate:
            upgrade(self.engine)

    @classmethod
    def at(cls, path: Path) -> Database:
        return cls(sqlite_url(path))

    @contextmanager
    def session(self) -> Iterator[Session]:
        """一个事务：正常结束提交，异常回滚。"""
        session = self._sessions()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _sqlite_pragmas(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class IncompatibleDatabase(RuntimeError):
    """数据库不是由当前版本创建的。"""


def upgrade(engine: Engine) -> None:
    """升级到最新结构。不是由当前迁移脚本创建的数据库会被拒绝，而不是尝试转换。"""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    known = {script.revision for script in ScriptDirectory.from_config(config).walk_revisions()}
    with _migrate_lock, engine.begin() as connection:
        tables = set(inspect(connection).get_table_names())
        version = None
        if "alembic_version" in tables:
            version = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        if (tables - {"alembic_version"} and version is None) or (version is not None and version not in known):
            raise IncompatibleDatabase(
                f"数据库 {engine.url.render_as_string(hide_password=True)} 由早期版本创建，无法使用。"
                "请移走或删除该文件（课程笔记文件不受影响）后重新启动。"
            )
        config.attributes["connection"] = connection
        command.upgrade(config, "head")

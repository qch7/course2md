"""工作区存储：课程与任务记录。

底层是 :class:`course2md.core.db.Database`（本地 SQLite，服务器可换 PostgreSQL），与阶段任务队列共用
同一个数据库。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..core.crypto import COURSE_FIELDS, JOB_FIELDS, FieldCipher
from ..core.db import Database
from ..core.models import CourseRow, JobRow


class WorkspaceStore:
    def __init__(self, path: Path | None = None, db: Database | None = None, cipher: FieldCipher | None = None) -> None:
        if db is None and path is None:
            raise ValueError("需要数据库路径或连接")
        self.db = db or Database.at(path)  # type: ignore[arg-type]
        self.path = path or Path(self.db.url.removeprefix("sqlite:///"))
        self.cipher = cipher or FieldCipher.from_env()

    def load_courses(self) -> list[dict]:
        with self.db.session() as session:
            return [
                self.cipher.map_fields(json.loads(row.data), COURSE_FIELDS, encrypt=False)
                for row in session.scalars(select(CourseRow))
            ]

    def load_jobs(self) -> list[dict]:
        with self.db.session() as session:
            return [
                self.cipher.map_fields(json.loads(row.data), JOB_FIELDS, encrypt=False)
                for row in session.scalars(select(JobRow))
            ]

    def save_course(self, course: dict) -> None:
        with self.db.session() as session:
            stored = self.cipher.map_fields(course, COURSE_FIELDS, encrypt=True)
            session.merge(CourseRow(id=course["id"], data=json.dumps(stored, ensure_ascii=False)))

    def delete_course(self, course_id: str) -> None:
        with self.db.session() as session:
            row = session.get(CourseRow, course_id)
            if row is not None:
                session.delete(row)

    def delete_job(self, job_id: str) -> None:
        with self.db.session() as session:
            row = session.get(JobRow, job_id)
            if row is not None:
                session.delete(row)

    def save_job(self, job: dict, replace_existing: bool = False) -> bool:
        """replace_existing 时同一输出目录的旧记录被替换；否则输出目录已被其它任务占用时不写入并返回 False。"""
        data = json.dumps(self.cipher.map_fields(job, JOB_FIELDS, encrypt=True), ensure_ascii=False, default=str)
        out_dir = str(job["out_dir"])
        with self.db.session() as session:
            if replace_existing:
                for row in session.scalars(select(JobRow).where(JobRow.out_dir == out_dir, JobRow.id != job["id"])):
                    session.delete(row)
                session.flush()
                session.merge(JobRow(id=job["id"], out_dir=out_dir, data=data))
                return True
            existing = session.get(JobRow, job["id"])
            if existing is not None:
                existing.data = data
                return True
            if self.db.is_sqlite:
                lite = sqlite_insert(JobRow).values(id=job["id"], out_dir=out_dir, data=data)
                result = cast(CursorResult[Any], session.execute(lite.on_conflict_do_nothing()))
                return result.rowcount > 0
            statement = postgres_insert(JobRow).values(id=job["id"], out_dir=out_dir, data=data)
            return session.scalar(statement.on_conflict_do_nothing().returning(JobRow.id)) is not None

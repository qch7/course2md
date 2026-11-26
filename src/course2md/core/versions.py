"""笔记版本历史：每次整理完成或对话修改后保存两版 Markdown 的快照（内容存于产物存储）。"""

from __future__ import annotations

import builtins
import time
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from ..storage import ArtifactStore
from .db import Database
from .models import NoteVersionRow


class NoteVersions:
    def __init__(self, db: Database, store: ArtifactStore) -> None:
        self.db = db
        self.store = store

    def record(self, job_id: str, detailed: Path | None, brief: Path | None, label: str, model: str = "") -> int | None:
        """保存当前两版笔记；内容与最新版本相同时不重复保存，返回版本号。"""
        if not detailed or not Path(detailed).is_file():
            return None
        detailed_digest = self.store.put_bytes(Path(detailed).read_bytes())
        brief_digest = self.store.put_bytes(Path(brief).read_bytes()) if brief and Path(brief).is_file() else ""
        with self.db.session() as session:
            latest = session.scalars(
                select(NoteVersionRow)
                .where(NoteVersionRow.job_id == job_id)
                .order_by(NoteVersionRow.version.desc())
                .limit(1)
            ).first()
            if latest is not None and (latest.detailed, latest.brief) == (detailed_digest, brief_digest):
                return latest.version
            version = (
                session.scalar(select(func.max(NoteVersionRow.version)).where(NoteVersionRow.job_id == job_id)) or 0
            ) + 1
            session.add(
                NoteVersionRow(
                    job_id=job_id,
                    version=version,
                    detailed=detailed_digest,
                    brief=brief_digest,
                    label=label,
                    model=model,
                    created_at=time.time(),
                )
            )
            return version

    def discard(self, job_id: str) -> None:
        """任务被永久删除时清除其版本记录（内容按摘要共享，不在此删除）。"""
        with self.db.session() as session:
            session.execute(delete(NoteVersionRow).where(NoteVersionRow.job_id == job_id))

    def list(self, job_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(NoteVersionRow).where(NoteVersionRow.job_id == job_id).order_by(NoteVersionRow.version.desc())
            )
            return [
                {
                    "version": row.version,
                    "label": row.label,
                    "model": row.model,
                    "created_at": row.created_at,
                    "has_brief": bool(row.brief),
                }
                for row in rows
            ]

    def text(self, job_id: str, version: int, kind: str) -> str | None:
        with self.db.session() as session:
            row = session.scalars(
                select(NoteVersionRow).where(NoteVersionRow.job_id == job_id, NoteVersionRow.version == version)
            ).first()
            if row is None:
                return None
            digest = row.detailed if kind == "detailed" else row.brief
        return self.store.get_bytes(digest).decode("utf-8") if digest else None

    def export_all(self, job_id: str) -> builtins.list[dict[str, Any]]:
        """按版本号升序导出任务的全部笔记版本及其原始字节。"""
        with self.db.session() as session:
            rows = list(
                session.scalars(
                    select(NoteVersionRow).where(NoteVersionRow.job_id == job_id).order_by(NoteVersionRow.version.asc())
                )
            )
        exported: builtins.list[dict[str, Any]] = []
        for row in rows:
            if not row.detailed or not self.store.exists(row.detailed):
                continue
            detailed = self.store.get_bytes(row.detailed)
            brief = self.store.get_bytes(row.brief) if row.brief and self.store.exists(row.brief) else None
            exported.append(
                {
                    "version": row.version,
                    "label": row.label,
                    "model": row.model,
                    "created_at": row.created_at,
                    "detailed_bytes": detailed,
                    "brief_bytes": brief,
                }
            )
        return exported

    def import_version(
        self,
        job_id: str,
        detailed_bytes: bytes,
        brief_bytes: bytes | None,
        label: str,
        model: str = "",
        created_at: float | None = None,
    ) -> int:
        """从产物包导入一条笔记版本快照，保留原标签、模型与时间。"""
        detailed_digest = self.store.put_bytes(detailed_bytes)
        brief_digest = self.store.put_bytes(brief_bytes) if brief_bytes is not None else ""
        with self.db.session() as session:
            version = (
                session.scalar(select(func.max(NoteVersionRow.version)).where(NoteVersionRow.job_id == job_id)) or 0
            ) + 1
            session.add(
                NoteVersionRow(
                    job_id=job_id,
                    version=version,
                    detailed=detailed_digest,
                    brief=brief_digest,
                    label=label,
                    model=model,
                    created_at=time.time() if created_at is None else float(created_at),
                )
            )
            return version

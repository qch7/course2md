"""笔记版本历史：每次整理完成或对话修改后保存两版 Markdown 的快照（内容存于产物存储）。"""

from __future__ import annotations

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

"""阶段任务队列：数据库表 + 租约。

节点领取任务后获得一段租约，执行期间定期续约；租约过期（节点崩溃或失联）的任务回到队列，
由其它节点接手，超过次数上限才标记失败。领取用带状态条件的 UPDATE 实现，SQLite 与
PostgreSQL 上都只会有一个节点抢到同一任务。
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, delete, or_, select, update

from ..storage import ArtifactStore, canonical_json
from .crypto import TASK_FIELDS, FieldCipher
from .db import Database
from .models import ArtifactRow, TaskEventRow, TaskRow, WorkerRow

DEFAULT_LEASE = 60.0
WORKER_ONLINE_SECONDS = 90.0


class LeaseLost(RuntimeError):
    """租约已被收回（超时转交或任务被取消后删除），节点应停止执行。"""


@dataclass
class ClaimedTask:
    id: str
    job_id: str
    stage: str
    payload: dict[str, Any]
    attempts: int
    lease_until: float


def satisfies(capabilities: set[str] | list[str], requires: list[str] | set[str]) -> bool:
    """能力是否满足需求。resolver:<n> 的需求也可由带版本的 resolver:<n>@<版本> 满足。"""
    names = set(capabilities) | {item.split("@", 1)[0] for item in capabilities}
    return set(requires) <= names


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> float:
    return time.time()


def task_dict(row: TaskRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "job_id": row.job_id,
        "stage": row.stage,
        "status": row.status,
        "requires": list(row.requires or []),
        "affinity": row.affinity,
        "cache_key": row.cache_key,
        "result": row.result,
        "warnings": list(row.warnings or []),
        "error": row.error,
        "error_type": row.error_type,
        "worker_id": row.worker_id,
        "lease_until": row.lease_until,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "cancel_requested": row.cancel_requested,
        "progress": {
            "stage": row.progress_stage,
            "fraction": row.progress_fraction,
            "message": row.progress_message,
            "sub": row.progress_sub,
            "seq": row.progress_seq,
        },
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
    }


class TaskQueue:
    def __init__(self, db: Database, store: ArtifactStore, cipher: FieldCipher | None = None) -> None:
        self.db = db
        self.store = store
        self.cipher = cipher or FieldCipher.from_env()

    # ------------------------------------------------------------ 提交与查询
    def enqueue(
        self,
        job_id: str,
        stage: str,
        payload: dict[str, Any],
        requires: list[str] | tuple[str, ...] = (),
        cache_key: str = "",
        affinity: str | None = None,
        max_attempts: int = 3,
        owner_id: str | None = None,
    ) -> str:
        now = _now()
        task_id = uuid.uuid4().hex
        with self.db.session() as session:
            session.add(
                TaskRow(
                    id=task_id,
                    job_id=job_id,
                    owner_id=owner_id,
                    stage=stage,
                    status="pending",
                    requires=sorted(set(requires)),
                    affinity=affinity,
                    payload=self.cipher.map_fields(payload, TASK_FIELDS, encrypt=True),
                    cache_key=cache_key,
                    warnings=[],
                    error="",
                    error_type="",
                    attempts=0,
                    max_attempts=max_attempts,
                    cancel_requested=False,
                    progress_stage="",
                    progress_fraction=0.0,
                    progress_message="",
                    progress_seq=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        return task_id

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.db.session() as session:
            row = session.get(TaskRow, task_id)
            return task_dict(row) if row else None

    def tasks_for_job(self, job_id: str) -> list[dict[str, Any]]:
        with self.db.session() as session:
            rows = session.scalars(select(TaskRow).where(TaskRow.job_id == job_id).order_by(TaskRow.created_at))
            return [task_dict(row) for row in rows]

    def find_reusable(self, cache_key: str) -> dict[str, Any] | None:
        """同一缓存键最近一次成功的执行，其输出可直接复用。"""
        if not cache_key:
            return None
        with self.db.session() as session:
            row = session.scalars(
                select(TaskRow)
                .where(TaskRow.cache_key == cache_key, TaskRow.status == "done")
                .order_by(TaskRow.finished_at.desc())
                .limit(1)
            ).first()
            if row is None or not row.result or not self.store.exists(row.result):
                return None
            return task_dict(row)

    def find_for_run(self, job_id: str, run_id: str, cache_key: str) -> dict[str, Any] | None:
        """重启后接回本轮任务，避免重复派发仍在执行或已经完成的阶段。"""
        if not run_id:
            return None
        with self.db.session() as session:
            rows = session.scalars(
                select(TaskRow)
                .where(
                    TaskRow.job_id == job_id,
                    TaskRow.cache_key == cache_key,
                )
                .order_by(TaskRow.created_at.desc())
            )
            for row in rows:
                if row.payload.get("run_id") == run_id:
                    return task_dict(row)
        return None

    def events(self, task_id: str, after: int = 0) -> list[tuple[int, dict[str, Any]]]:
        with self.db.session() as session:
            rows = session.scalars(
                select(TaskEventRow)
                .where(TaskEventRow.task_id == task_id, TaskEventRow.id > after)
                .order_by(TaskEventRow.id)
            )
            return [(row.id, dict(row.data)) for row in rows]

    def output(self, task: dict[str, Any]) -> Any:
        return self.store.get_json(task["result"])  # type: ignore[attr-defined]

    # ------------------------------------------------------------ 节点
    def register_worker(
        self, worker_id: str, name: str, capabilities: list[str] | set[str], kind: str = "builtin", version: str = ""
    ) -> None:
        now = _now()
        with self.db.session() as session:
            row = session.get(WorkerRow, worker_id)
            if row is None:
                row = WorkerRow(id=worker_id, created_at=now, disabled=False)
                session.add(row)
            row.name, row.version = name or row.name, version
            if row.token_hash is None:
                row.kind = kind
            row.capabilities = sorted(set(capabilities))
            row.last_seen = now

    def touch_worker(self, worker_id: str) -> None:
        with self.db.session() as session:
            session.execute(update(WorkerRow).where(WorkerRow.id == worker_id).values(last_seen=_now()))

    def create_worker_token(self, name: str, owner_id: str | None = None) -> tuple[str, str]:
        """为远程节点生成令牌，返回 (节点编号, 令牌)。数据库只保存令牌的 SHA-256。"""
        worker_id = f"node-{uuid.uuid4().hex[:12]}"
        token = f"c2m_{secrets.token_urlsafe(32)}"
        now = _now()
        with self.db.session() as session:
            session.add(
                WorkerRow(
                    id=worker_id,
                    name=name,
                    kind="remote",
                    capabilities=[],
                    version="",
                    token_hash=hash_token(token),
                    owner_id=owner_id,
                    last_seen=0.0,
                    created_at=now,
                    disabled=False,
                )
            )
        return worker_id, token

    def authenticate_worker(self, token: str) -> dict[str, Any] | None:
        if not token:
            return None
        with self.db.session() as session:
            row = session.scalars(select(WorkerRow).where(WorkerRow.token_hash == hash_token(token))).first()
            if row is None or row.disabled:
                return None
            return {"id": row.id, "name": row.name, "owner_id": row.owner_id}

    def disable_worker(self, worker_id: str) -> bool:
        with self.db.session() as session:
            row = session.get(WorkerRow, worker_id)
            if row is None or row.kind == "builtin":
                return False
            row.disabled, row.token_hash, row.last_seen = True, None, 0.0
        return True

    def delete_worker(self, worker_id: str) -> bool:
        with self.db.session() as session:
            row = session.get(WorkerRow, worker_id)
            if row is None or row.kind == "builtin":
                return False
            active = session.scalar(
                select(TaskRow.id)
                .where(
                    TaskRow.status.in_(("pending", "leased")),
                    or_(TaskRow.worker_id == worker_id, TaskRow.affinity == worker_id),
                )
                .limit(1)
            )
            if active is not None:
                raise ValueError("节点仍有未完成任务，请等待完成或取消任务后再删除")
            session.delete(row)
        return True

    def leave(self, worker_id: str) -> None:
        """节点正常退出：立即标记离线，等待中的任务会提示节点不在线。"""
        with self.db.session() as session:
            session.execute(update(WorkerRow).where(WorkerRow.id == worker_id).values(last_seen=0.0))

    def workers(self) -> list[dict[str, Any]]:
        now = _now()
        with self.db.session() as session:
            rows = session.scalars(select(WorkerRow).order_by(WorkerRow.created_at))
            return [
                {
                    "id": row.id,
                    "name": row.name,
                    "kind": row.kind,
                    "capabilities": list(row.capabilities or []),
                    "version": row.version,
                    "last_seen": row.last_seen,
                    "disabled": row.disabled,
                    "owner_id": row.owner_id,
                    "online": not row.disabled and now - row.last_seen < WORKER_ONLINE_SECONDS,
                }
                for row in rows
            ]

    def capable_worker_online(
        self, requires: list[str], affinity: str | None = None, owner_id: str | None = None
    ) -> bool:
        """owner_id 为任务所属用户时，只计入能领取该任务的节点（共享节点与该用户的个人节点）。"""
        return any(
            item["online"]
            and satisfies(item["capabilities"], requires)
            and (not affinity or item["id"] == affinity)
            and (owner_id is None or item["owner_id"] in (None, owner_id))
            for item in self.workers()
        )

    # ------------------------------------------------------------ 清理
    def discard_job(self, job_id: str) -> int:
        """删除某个任务的全部阶段记录、运行输出与产物记录（产物内容按摘要共享，不在此删除）。"""
        with self.db.session() as session:
            ids = list(session.scalars(select(TaskRow.id).where(TaskRow.job_id == job_id)))
            if ids:
                session.execute(delete(TaskEventRow).where(TaskEventRow.task_id.in_(ids)))
                session.execute(delete(ArtifactRow).where(ArtifactRow.task_id.in_(ids)))
                session.execute(delete(TaskRow).where(TaskRow.id.in_(ids)))
            return len(ids)

    # ------------------------------------------------------------ 领取与租约
    def claim(
        self, worker_id: str, capabilities: set[str] | list[str], lease_seconds: float = DEFAULT_LEASE
    ) -> ClaimedTask | None:
        """领取最早的、能力满足且没有绑定其它节点的待执行任务。"""
        self.reap()
        caps = set(capabilities)
        with self.db.session() as session:
            worker = session.get(WorkerRow, worker_id)
            owner = worker.owner_id if worker is not None else None
            query = select(TaskRow).where(
                TaskRow.status == "pending",
                TaskRow.cancel_requested.is_(False),
                or_(TaskRow.affinity.is_(None), TaskRow.affinity == worker_id),
            )
            if owner is not None:  # 个人节点只领取主人的任务，共享节点（无归属）可领取全部
                query = query.where(TaskRow.owner_id == owner)
            candidates = session.scalars(query.order_by(TaskRow.created_at)).all()
            for row in candidates:
                if not satisfies(caps, row.requires or []):
                    continue
                now = _now()
                lease_until = now + lease_seconds
                attempts, job_id, stage = row.attempts + 1, row.job_id, row.stage
                payload = self.cipher.map_fields(dict(row.payload), TASK_FIELDS, encrypt=False)
                claimed = session.execute(
                    update(TaskRow)
                    .where(TaskRow.id == row.id, TaskRow.status == "pending")
                    .values(
                        status="leased",
                        worker_id=worker_id,
                        lease_until=lease_until,
                        attempts=attempts,
                        started_at=now,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if cast(CursorResult, claimed).rowcount:
                    session.commit()
                    return ClaimedTask(row.id, job_id, stage, payload, attempts, lease_until)
        return None

    def renew(self, task_id: str, worker_id: str, lease_seconds: float = DEFAULT_LEASE) -> bool:
        """续约，返回是否已请求取消。租约已不属于该节点时抛出 LeaseLost。"""
        now = _now()
        with self.db.session() as session:
            updated = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.worker_id == worker_id,
                    TaskRow.status == "leased",
                    TaskRow.lease_until >= now,
                )
                .values(lease_until=now + lease_seconds, updated_at=now)
            )
            row = session.get(TaskRow, task_id)
            if not cast(CursorResult, updated).rowcount or row is None:
                raise LeaseLost(f"任务 {task_id} 的租约已失效")
            return bool(row.cancel_requested)

    def release(self, task_id: str, worker_id: str) -> None:
        """正常停机时归还租约，下次启动可继续；用户取消仍优先。"""
        with self.db.session() as session:
            session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.worker_id == worker_id,
                    TaskRow.status == "leased",
                )
                .values(status="pending", worker_id=None, lease_until=None, updated_at=_now())
            )
            session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.status == "pending",
                    TaskRow.cancel_requested.is_(True),
                )
                .values(status="cancelled", error="任务已取消", error_type="Cancelled", finished_at=_now())
            )

    def unbind(self, task_id: str, requires: list[str]) -> None:
        """原节点离线时，允许可重新获取输入的其它节点接手待执行阶段。"""
        with self.db.session() as session:
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.status == "pending")
                .values(
                    affinity=None,
                    requires=requires,
                    updated_at=_now(),
                )
            )

    def progress(
        self, task_id: str, worker_id: str, stage: str, fraction: float, message: str, sub: float | None = None
    ) -> None:
        with self.db.session() as session:
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.worker_id == worker_id)
                .values(
                    progress_stage=stage,
                    progress_fraction=fraction,
                    progress_message=message,
                    progress_sub=sub,
                    progress_seq=TaskRow.progress_seq + 1,
                    updated_at=_now(),
                )
            )

    def add_event(self, task_id: str, event: dict[str, Any]) -> None:
        with self.db.session() as session:
            session.add(TaskEventRow(task_id=task_id, data=event))

    def complete(
        self,
        task_id: str,
        worker_id: str,
        output: Any,
        warnings: list[str] | None = None,
        model: str = "",
        publish: Callable[[], None] | None = None,
    ) -> str:
        """保存输出产物并结束任务；publish 在校验租约并锁定任务行后发布暂存文件。"""
        data = canonical_json(output)
        digest = self.store.put_bytes(data)
        now = _now()
        with self.db.session() as session:
            row = session.get(TaskRow, task_id)
            if row is None or row.worker_id != worker_id or row.status != "leased":
                raise LeaseLost(f"任务 {task_id} 的租约已失效")
            changed = session.execute(
                update(TaskRow)
                .where(
                    TaskRow.id == task_id,
                    TaskRow.worker_id == worker_id,
                    TaskRow.status == "leased",
                    TaskRow.lease_until >= now,
                    TaskRow.cancel_requested.is_(False),
                )
                .values(
                    status="done",
                    result=digest,
                    warnings=list(warnings or []),
                    finished_at=now,
                    updated_at=now,
                    lease_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            if not cast(CursorResult, changed).rowcount:
                raise LeaseLost(f"任务 {task_id} 的租约已失效或已取消")
            if publish is not None:
                publish()
            kind = output.get("kind", "") if isinstance(output, dict) else "list"
            session.add(
                ArtifactRow(
                    digest=digest,
                    kind=str(kind),
                    job_id=row.job_id,
                    task_id=row.id,
                    stage=row.stage,
                    worker_id=worker_id,
                    model=model,
                    size=len(data),
                    created_at=now,
                )
            )
        return digest

    def fail(
        self,
        task_id: str,
        worker_id: str,
        error: str,
        error_type: str = "",
        cancelled: bool = False,
        warnings: list[str] | None = None,
    ) -> None:
        now = _now()
        with self.db.session() as session:
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.worker_id == worker_id, TaskRow.status == "leased")
                .values(
                    status="cancelled" if cancelled else "failed",
                    error=error[:4000],
                    error_type=error_type,
                    warnings=list(warnings or []),
                    finished_at=now,
                    updated_at=now,
                    lease_until=None,
                )
            )

    def cancel(self, task_id: str) -> None:
        """请求取消：未领取的直接结束，执行中的由节点在下次续约时得知。"""
        now = _now()
        with self.db.session() as session:
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.status == "pending")
                .values(
                    status="cancelled",
                    error="任务已取消",
                    error_type="Cancelled",
                    cancel_requested=True,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.execute(
                update(TaskRow)
                .where(TaskRow.id == task_id, TaskRow.status == "leased")
                .values(cancel_requested=True, updated_at=now)
            )

    def reap(self) -> int:
        """收回过期租约：未超过次数上限的回到队列，否则标记失败。"""
        now = _now()
        with self.db.session() as session:
            expired = session.scalars(
                select(TaskRow).where(TaskRow.status == "leased", TaskRow.lease_until < now)
            ).all()
            count = 0
            for row in expired:
                values: dict[str, Any] = {"lease_until": None, "updated_at": now}
                if row.cancel_requested:
                    values.update(status="cancelled", error="任务已取消", error_type="Cancelled", finished_at=now)
                elif row.attempts >= row.max_attempts:
                    values.update(
                        status="failed",
                        error="执行节点多次失联，任务已停止",
                        error_type="LeaseExpired",
                        finished_at=now,
                    )
                else:
                    values.update(
                        status="pending", worker_id=None, progress_message=f"节点 {row.worker_id} 失联，等待重新分配"
                    )
                changed = session.execute(
                    update(TaskRow)
                    .where(
                        TaskRow.id == row.id,
                        TaskRow.status == "leased",
                        TaskRow.lease_until == row.lease_until,
                        TaskRow.cancel_requested == row.cancel_requested,
                    )
                    .values(**values)
                    .execution_options(synchronize_session=False)
                )
                count += cast(CursorResult, changed).rowcount
            return count

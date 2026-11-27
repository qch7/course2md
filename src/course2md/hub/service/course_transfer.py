"""课程产物打包与 Hub 间互传。"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ...core.accounts import LOCAL_USER, User
from ...jobs.manager import (
    ACTIVE_STATUSES,
    Job,
    JobManager,
)
from ...jobs.records import CourseRecord
from ...util.text import safe_name
from .common import NotFound, get_course, get_job, output_root, server_mode, visible


def _peer_store(manager: JobManager):
    from ..transfer import TransferPeerStore

    return manager.shared(
        "transfer-peers",
        lambda: TransferPeerStore(manager.data_dir / "hub-peers.json", cipher=manager.store.cipher),
    )


def _local_origins(manager: JobManager) -> set[str]:
    links = getattr(manager, "hub_links", None)
    return links.local_origin_urls() if links is not None else set()


def _resolve_remote_target(manager: JobManager, body: dict[str, Any], user: User) -> tuple[str, str, str]:
    from ..transfer import TransferError, validate_remote_hub_url

    peer_id = str(body.get("peer_id") or "").strip()
    raw_url = str(body.get("hub_url") or "").strip()
    raw_token = str(body.get("token") or "").strip()
    peer_name = ""

    if peer_id:
        peer = _peer_store(manager).get_for(user.id, peer_id)
        if peer is None:
            raise NotFound("对端 Hub 配置不存在")
        raw_url = raw_url or peer.hub_url
        raw_token = raw_token or peer.token
        peer_name = peer.name or peer.hub_url

    if not raw_url:
        raise TransferError("请先填写或选择一个对端 Hub 地址", status_code=400)

    hub_url = validate_remote_hub_url(
        raw_url,
        server_mode=server_mode(manager),
        is_admin=user.is_admin,
        local_origins=_local_origins(manager),
    )
    return hub_url, raw_token, peer_name or hub_url


def export_job_bundle_file(
    manager: JobManager, job_id: str, user: User = LOCAL_USER, *, include_cache: bool = False
) -> tuple[Path, str]:
    from ...jobs.bundle import export_bundle

    job = get_job(manager, job_id, user)
    course = manager.courses.get(job.course_id) if job.course_id else None
    if course is not None and not visible(user, course.get("owner")):
        course = None

    fd, tmp_name = tempfile.mkstemp(prefix="c2m-job-bundle-", suffix=".c2m.zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        export_bundle(manager, [job], tmp_path, course=course, include_cache=include_cache)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    stem = safe_name(f"{job.course}-{job.title}" if job.course else job.title, "course")
    return tmp_path, f"{stem}.c2m.zip"


def export_course_bundle_file(
    manager: JobManager,
    course_id: str,
    job_ids: list[str] | None = None,
    user: User = LOCAL_USER,
    *,
    include_cache: bool = False,
) -> tuple[Path, str]:
    from ...jobs.bundle import export_bundle

    course = get_course(manager, course_id, user)
    with manager.locked():
        owned_jobs = [
            job
            for job in manager.jobs.values()
            if job.course_id == course["id"] and visible(user, job.owner) and job.deleted_at is None
        ]
    if job_ids:
        wanted = [str(item).strip() for item in job_ids if str(item).strip()]
        by_id = {job.id: job for job in owned_jobs}
        missing = [jid for jid in wanted if jid not in by_id]
        if missing:
            raise NotFound("所选课次不存在或不属于该课程")
        selected = [by_id[jid] for jid in wanted]
    else:
        selected = [
            job
            for job in owned_jobs
            if job.status not in ACTIVE_STATUSES and not job.pending_messages and job.out_dir.is_dir()
        ]
    if not selected:
        raise ValueError("该课程目前没有可导出或传输的已完成课次")
    selected.sort(key=lambda item: item.started_at)

    fd, tmp_name = tempfile.mkstemp(prefix="c2m-course-bundle-", suffix=".c2m.zip")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        export_bundle(manager, selected, tmp_path, course=course, include_cache=include_cache)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    stem = safe_name(str(course.get("title") or course.get("course") or "course"), "course")
    return tmp_path, f"{stem}.c2m.zip"


def import_bundle_file(
    manager: JobManager,
    bundle_path: Path,
    *,
    user: User = LOCAL_USER,
    conflict: str = "skip",
    out_dir: str | None = None,
    origin_label: str = "产物包",
) -> dict[str, Any]:
    from ...jobs.bundle import import_bundle

    root = output_root(manager, user, out_dir)
    return import_bundle(
        manager,
        bundle_path,
        owner=user.id,
        root=root,
        conflict=conflict,
        server_mode=server_mode(manager),
        is_admin=user.is_admin,
        origin_label=origin_label,
    )


def list_transfer_peers(manager: JobManager, user: User = LOCAL_USER) -> dict[str, Any]:
    return {"peers": _peer_store(manager).list_for(user.id)}


def create_transfer_peer(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from ..transfer import validate_remote_hub_url

    hub_url = validate_remote_hub_url(
        str(body.get("hub_url") or ""),
        server_mode=server_mode(manager),
        is_admin=user.is_admin,
        local_origins=_local_origins(manager),
    )
    peer = _peer_store(manager).add(
        user.id,
        hub_url,
        token=str(body.get("token") or ""),
        name=str(body.get("name") or ""),
    )
    return {"peer": peer}


def update_transfer_peer(
    manager: JobManager, peer_id: str, body: dict[str, Any], user: User = LOCAL_USER
) -> dict[str, Any]:
    from ..transfer import validate_remote_hub_url

    hub_url = None
    if "hub_url" in body and body["hub_url"] is not None:
        hub_url = validate_remote_hub_url(
            str(body["hub_url"]),
            server_mode=server_mode(manager),
            is_admin=user.is_admin,
            local_origins=_local_origins(manager),
        )
    token = str(body["token"]) if "token" in body and body["token"] is not None else None
    name = str(body["name"]) if "name" in body and body["name"] is not None else None
    try:
        peer = _peer_store(manager).update(user.id, peer_id, hub_url=hub_url, token=token, name=name)
    except KeyError as exc:
        raise NotFound("对端 Hub 配置不存在") from exc
    return {"peer": peer}


def delete_transfer_peer(manager: JobManager, peer_id: str, user: User = LOCAL_USER) -> dict[str, Any]:
    try:
        _peer_store(manager).delete(user.id, peer_id)
    except KeyError as exc:
        raise NotFound("对端 Hub 配置不存在") from exc
    return {"ok": True}


def remote_catalog(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from ..transfer import RemoteHubClient

    hub_url, token, _ = _resolve_remote_target(manager, body, user)
    with RemoteHubClient(hub_url, token) as remote:
        return remote.catalog()


def transfer_push(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:  # noqa: PLR0915  历史遗留的长函数，拆分后移除
    from ...jobs.bundle import CONFLICT_MODES, export_bundle
    from ..transfer import RemoteHubClient

    hub_url, token, _ = _resolve_remote_target(manager, body, user)
    conflict = str(body.get("conflict") or "skip")
    if conflict not in CONFLICT_MODES:
        raise ValueError(f"不支持的冲突处理策略：{conflict!r}，可选 {', '.join(CONFLICT_MODES)}")
    include_cache = bool(body.get("include_cache", False))

    raw_job_ids = body.get("job_ids")
    course_id = str(body.get("course_id") or "").strip()

    groups: list[tuple[CourseRecord | None, list[Job]]] = []
    if isinstance(raw_job_ids, list) and raw_job_ids:
        by_course: dict[str, list[Job]] = {}
        for jid in dict.fromkeys(str(item).strip() for item in raw_job_ids if str(item).strip()):
            job = get_job(manager, jid, user)
            if course_id and job.course_id != course_id:
                raise ValueError(f"课次「{job.title}」不属于指定课程")
            by_course.setdefault(job.course_id or f"standalone:{job.id}", []).append(job)
        for key, jobs in by_course.items():
            course = manager.courses.get(key) if not key.startswith("standalone:") else None
            if course is not None and not visible(user, course.get("owner")):
                course = None
            groups.append((course, jobs))
    elif course_id:
        course = get_course(manager, course_id, user)
        with manager.locked():
            jobs = [
                job
                for job in manager.jobs.values()
                if job.course_id == course["id"]
                and visible(user, job.owner)
                and job.deleted_at is None
                and job.status not in ACTIVE_STATUSES
                and not job.pending_messages
                and job.out_dir.is_dir()
            ]
        if not jobs:
            raise ValueError("该课程没有可推送的已完成课次")
        jobs.sort(key=lambda item: item.started_at)
        groups.append((course, jobs))
    else:
        raise ValueError("请先选择要推送的课程或课次")

    courses_by_id: dict[str, dict[str, Any]] = {}
    imported: list[dict[str, Any]] = []
    overwritten: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    with RemoteHubClient(hub_url, token) as remote:
        for course, jobs in groups:
            fd, tmp_name = tempfile.mkstemp(prefix="c2m-push-", suffix=".c2m.zip")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                export_bundle(manager, jobs, tmp_path, course=course, include_cache=include_cache)
                result = remote.push_bundle(tmp_path, conflict=conflict)
            finally:
                tmp_path.unlink(missing_ok=True)
            remote_course = result.get("course")
            if isinstance(remote_course, dict) and remote_course.get("id"):
                courses_by_id[str(remote_course["id"])] = remote_course
            imported.extend(result.get("imported") or [])
            overwritten.extend(result.get("overwritten") or [])
            skipped.extend(result.get("skipped") or [])
            warnings.extend(result.get("warnings") or [])

    return {
        "direction": "push",
        "hub_url": hub_url,
        "courses": list(courses_by_id.values()),
        "imported": imported,
        "overwritten": overwritten,
        "skipped": skipped,
        "warnings": warnings,
    }


def transfer_pull(manager: JobManager, body: dict[str, Any], user: User = LOCAL_USER) -> dict[str, Any]:
    from ...jobs.bundle import CONFLICT_MODES
    from ..transfer import RemoteHubClient

    hub_url, token, peer_name = _resolve_remote_target(manager, body, user)
    conflict = str(body.get("conflict") or "skip")
    if conflict not in CONFLICT_MODES:
        raise ValueError(f"不支持的冲突处理策略：{conflict!r}，可选 {', '.join(CONFLICT_MODES)}")

    include_cache = bool(body.get("include_cache", False))
    course_id = str(body.get("course_id") or "").strip()
    raw_job_ids = body.get("job_ids")
    job_ids = (
        list(dict.fromkeys(str(item).strip() for item in raw_job_ids if str(item).strip()))
        if isinstance(raw_job_ids, list)
        else []
    )
    if not course_id and not job_ids:
        raise ValueError("请先选择要拉取的课程或课次")

    out_dir = str(body.get("out_dir") or "").strip() or None
    courses_by_id: dict[str, dict[str, Any]] = {}
    imported: list[dict[str, Any]] = []
    overwritten: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    def _absorb(result: dict[str, Any]) -> None:
        item_course = result.get("course")
        if isinstance(item_course, dict) and item_course.get("id"):
            courses_by_id[str(item_course["id"])] = item_course
        imported.extend(result.get("imported") or [])
        overwritten.extend(result.get("overwritten") or [])
        skipped.extend(result.get("skipped") or [])
        warnings.extend(result.get("warnings") or [])

    with RemoteHubClient(hub_url, token) as remote:
        if course_id:
            fd, tmp_name = tempfile.mkstemp(prefix="c2m-pull-", suffix=".c2m.zip")
            os.close(fd)
            tmp_path = Path(tmp_name)
            try:
                remote.download_course_bundle(course_id, tmp_path, job_ids=job_ids or None, include_cache=include_cache)
                _absorb(
                    import_bundle_file(
                        manager,
                        tmp_path,
                        user=user,
                        conflict=conflict,
                        out_dir=out_dir,
                        origin_label=f"对端 Hub（{peer_name}）",
                    )
                )
            finally:
                tmp_path.unlink(missing_ok=True)
        else:
            for jid in job_ids:
                fd, tmp_name = tempfile.mkstemp(prefix="c2m-pull-", suffix=".c2m.zip")
                os.close(fd)
                tmp_path = Path(tmp_name)
                try:
                    remote.download_job_bundle(jid, tmp_path, include_cache=include_cache)
                    _absorb(
                        import_bundle_file(
                            manager,
                            tmp_path,
                            user=user,
                            conflict=conflict,
                            out_dir=out_dir,
                            origin_label=f"对端 Hub（{peer_name}）",
                        )
                    )
                finally:
                    tmp_path.unlink(missing_ok=True)

    return {
        "direction": "pull",
        "hub_url": hub_url,
        "courses": list(courses_by_id.values()),
        "imported": imported,
        "overwritten": overwritten,
        "skipped": skipped,
        "warnings": warnings,
    }

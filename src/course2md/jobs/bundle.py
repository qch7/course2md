"""课程产物包（.c2m.zip）的打包与导入。

用于离线备份分享，也作为 Hub 间在线互传的统一数据格式。
打包时剥离来源凭证与本机临时文件，把任务目录内的绝对路径改写为 ``job://`` 相对引用；
导入时校验归档完整性与路径边界，在目标 Hub 还原课程、课次、笔记、配图、对话上下文与版本历史。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .. import __version__
from ..agents import AgentConfig
from ..config import LlmConfig
from ..pipeline import JobResult
from ..resolvers import public_source
from ..stages.codec import options_from_dict, options_to_dict
from ..util.job_refs import from_job_refs, resolve_relative, to_job_refs
from ..util.text import safe_name
from .job import ACTIVE_STATUSES, Job, collect_files
from .manager import JobManager
from .records import CourseRecord, MessageRecord
from .stage_cache import collect_stage_cache, restore_stage_cache

BUNDLE_FORMAT = "course2md-bundle"
BUNDLE_VERSION = 1
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_BUNDLE_FILES = 5000
CONFLICT_MODES = ("skip", "overwrite", "copy")

_SKIP_TOP_DIRS = frozenset({"media", "audio", ".course2md"})
_SKIP_SUFFIXES = (".wav", ".part", ".lock")
_SKIP_WORKSPACE_DIRS = frozenset({"turns", "vision"})
_SKIP_WORKSPACE_FILES = frozenset({"agent-session.json"})


def _should_pack(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if parts[0] in _SKIP_TOP_DIRS:
        return False
    if relative.name.endswith(_SKIP_SUFFIXES):
        return False
    if "agent-workspace" in parts:
        idx = parts.index("agent-workspace")
        tail = parts[idx + 1 :]
        if not tail:
            return False
        if relative.name in _SKIP_WORKSPACE_FILES:
            return False
        if any(segment in _SKIP_WORKSPACE_DIRS for segment in tail[:-1]):
            return False
    return True


def _sanitize_messages(messages: Sequence[Mapping[str, Any]]) -> list[MessageRecord]:
    cleaned: list[MessageRecord] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "done")
        if status in ACTIVE_STATUSES:
            status = "interrupted"
        entry: MessageRecord = {
            "id": str(item.get("id") or uuid.uuid4().hex),
            "role": str(item.get("role") or "user"),
            "text": str(item.get("text") or ""),
            "status": status,
        }
        if "updated" in item:
            entry["updated"] = bool(item["updated"])
        cleaned.append(entry)
    return cleaned


def _portable_file_bytes(path: Path, relative: PurePosixPath, out_dir: Path) -> bytes:
    """读取待打包文件，对 job.json 与 context.json 做路径便携化与来源脱敏。"""
    raw = path.read_bytes()
    if relative.as_posix() == "job.json":
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return raw
        if isinstance(data, dict):
            if isinstance(data.get("source"), str) and data["source"]:
                data["source"] = public_source(data["source"])
            data = to_job_refs(data, out_dir)
            return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return raw
    if relative.name == "context.json" and "agent-workspace" in relative.parts:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return raw
        if isinstance(data, dict) and isinstance(data.get("figures"), dict):
            data["figures"] = to_job_refs(data["figures"], out_dir)
            return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return raw
    return raw


def _serialize_course(course: CourseRecord | None, jobs: list[Job]) -> dict[str, Any] | None:
    if course is None:
        first = jobs[0] if jobs else None
        if first is None:
            return None
        title = first.course or first.title or "导入课程"
        return {
            "origin_id": first.course_id or "",
            "title": title,
            "course": title,
            "source": public_source(first.source) if first.source else "",
            "resolver": "",
            "resolver_label": "",
            "view_options": [],
            "term_start": "",
            "term_source": "",
            "updated_at": time.time(),
            "sessions": [],
        }
    sessions: list[dict[str, Any]] = []
    for session in course.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        folder = str(session.get("folder") or "")
        folder_name = Path(folder).name if folder else ""
        sessions.append(
            {
                "id": str(session.get("id") or ""),
                "date": str(session.get("date") or ""),
                "title": str(session.get("title") or ""),
                "views": session.get("views"),
                "week": session.get("week"),
                "course": str(session.get("course") or course.get("course") or course.get("title") or ""),
                "meta": dict(session.get("meta") or {}),
                "folder_name": folder_name,
            }
        )
    source = str(course.get("source") or "")
    return {
        "origin_id": str(course.get("id") or ""),
        "title": str(course.get("title") or course.get("course") or "课程"),
        "course": str(course.get("course") or course.get("title") or "课程"),
        "source": public_source(source) if source else "",
        "resolver": str(course.get("resolver") or ""),
        "resolver_label": str(course.get("resolver_label") or ""),
        "view_options": list(course.get("view_options") or []),
        "term_start": str(course.get("term_start") or ""),
        "term_source": str(course.get("term_source") or ""),
        "updated_at": float(course.get("updated_at") or time.time()),
        "sessions": sessions,
    }


def export_bundle(  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
    manager: JobManager,
    jobs: list[Job],
    target_path: Path,
    *,
    course: CourseRecord | None = None,
    include_cache: bool = False,
) -> dict[str, Any]:
    """将一个或多个已完成课次任务打包为便携的 .c2m.zip 产物包。

    include_cache 为 True 时同时带上各阶段的输出（转写、画面、校对结果等），
    目标 Hub 导入后可以直接从中间阶段重新运行，不必重新下载与转写。
    """
    if not jobs:
        raise ValueError("没有可导出或传输的课次产物")
    for job in jobs:
        with job.locked():
            if job.status in ACTIVE_STATUSES or job.pending_messages:
                raise ValueError(f"课次「{job.title}」正在处理中，请等待完成后再导出或传输")
            if not job.out_dir.is_dir():
                raise ValueError(f"课次「{job.title}」的输出目录不存在，无法打包")

    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target_path.parent, prefix=".c2m-bundle-", suffix=".zip")
    os.close(fd)
    total_files = 0
    total_bytes = 0
    job_entries: list[dict[str, Any]] = []

    try:
        with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for index, job in enumerate(jobs):
                key = f"job-{index}"
                with job.locked():
                    out_dir = job.out_dir.resolve()
                    job_source = public_source(job.source) if job.source else ""
                    serialized_options: dict[str, Any] | None = None
                    if job.options is not None:
                        raw_opts = options_to_dict(job.options)
                        if isinstance(raw_opts.get("source"), str) and raw_opts["source"]:
                            raw_opts["source"] = public_source(raw_opts["source"])
                        serialized_options = to_job_refs(raw_opts, out_dir)
                    serialized_result: dict[str, Any] | None = None
                    if job.result is not None:
                        raw_result = {
                            name: str(getattr(job.result, name)) if getattr(job.result, name) else None
                            for name in ("notes", "detailed_notes", "brief_notes", "note_workspace")
                        }
                        serialized_result = to_job_refs(raw_result, out_dir)
                    job_meta: dict[str, Any] = {
                        "key": key,
                        "origin_id": job.id,
                        "title": job.title,
                        "course": job.course,
                        "session_id": job.session_id,
                        "session_date": job.session_date,
                        "week": job.week,
                        "folder_name": out_dir.name,
                        "source": job_source,
                        "status": job.status if job.status not in ACTIVE_STATUSES else "done",
                        "stage": job.stage,
                        "progress": job.progress,
                        "message": job.message,
                        "error": job.error,
                        "agent": job.agent,
                        "agent_model": job.agent_model,
                        "timings": dict(job.timings),
                        "started_at": job.started_at,
                        "messages": _sanitize_messages(job.messages),
                        "revision": job.revision,
                        "options": serialized_options,
                        "result": serialized_result,
                        "versions": [],
                        "files": [],
                    }

                file_records: list[dict[str, Any]] = []
                for path in sorted(out_dir.rglob("*")):
                    if path.is_symlink() or not path.is_file():
                        continue
                    try:
                        resolved = path.resolve()
                    except OSError:
                        continue
                    if not resolved.is_relative_to(out_dir):
                        continue
                    rel = PurePosixPath(path.relative_to(out_dir).as_posix())
                    if not _should_pack(rel):
                        continue
                    payload = _portable_file_bytes(path, rel, out_dir)
                    total_files += 1
                    total_bytes += len(payload)
                    if total_files > MAX_BUNDLE_FILES:
                        raise ValueError(f"产物包文件数量超过上限（{MAX_BUNDLE_FILES}）")
                    if total_bytes > MAX_UNCOMPRESSED_BYTES:
                        raise ValueError("产物包内容总大小超过上限（1 GB）")
                    arcname = f"jobs/{key}/files/{rel.as_posix()}"
                    archive.writestr(arcname, payload)
                    file_records.append(
                        {
                            "rel": rel.as_posix(),
                            "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest(),
                        }
                    )
                if not file_records:
                    raise ValueError(f"课次「{job.title}」没有可打包的产物文件")
                job_meta["files"] = file_records

                versions = getattr(manager, "versions", None)
                if versions is not None:
                    version_entries: list[dict[str, Any]] = []
                    for ver in versions.export_all(job.id):
                        num = int(ver["version"])
                        detailed_arc = f"jobs/{key}/versions/v{num}.detailed.md"
                        detailed_bytes: bytes = ver["detailed_bytes"]
                        archive.writestr(detailed_arc, detailed_bytes)
                        brief_arc = ""
                        brief_bytes: bytes | None = ver["brief_bytes"]
                        if brief_bytes is not None:
                            brief_arc = f"jobs/{key}/versions/v{num}.brief.md"
                            archive.writestr(brief_arc, brief_bytes)
                        version_entries.append(
                            {
                                "version": num,
                                "label": str(ver["label"]),
                                "model": str(ver["model"]),
                                "created_at": float(ver["created_at"]),
                                "detailed_file": detailed_arc,
                                "brief_file": brief_arc,
                            }
                        )
                    job_meta["versions"] = version_entries

                if include_cache:
                    stage_entries: list[dict[str, Any]] = []
                    for meta, output in collect_stage_cache(manager, job):
                        payload = (json.dumps(output, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
                        total_files += 1
                        total_bytes += len(payload)
                        if total_files > MAX_BUNDLE_FILES or total_bytes > MAX_UNCOMPRESSED_BYTES:
                            raise ValueError("产物包内容超过上限，请取消「带上阶段缓存」后重试")
                        arcname = f"jobs/{key}/stages/{meta['stage']}.json"
                        archive.writestr(arcname, payload)
                        stage_entries.append(
                            {
                                **meta,
                                "file": arcname,
                                "sha256": hashlib.sha256(payload).hexdigest(),
                                "size": len(payload),
                            }
                        )
                    job_meta["stage_cache"] = stage_entries

                job_entries.append(job_meta)

            manifest = {
                "format": BUNDLE_FORMAT,
                "version": BUNDLE_VERSION,
                "exported_at": time.time(),
                "course2md_version": __version__,
                "course": _serialize_course(course, jobs),
                "jobs": job_entries,
            }
            archive.writestr("bundle.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

        if Path(tmp_name).stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError(f"产物包大小超过上限（{MAX_BUNDLE_BYTES // (1024 * 1024)} MB）")
        os.replace(tmp_name, target_path)
    finally:
        if Path(tmp_name).exists():
            Path(tmp_name).unlink(missing_ok=True)

    return {
        "path": target_path,
        "job_count": len(job_entries),
        "file_count": total_files,
        "size": target_path.stat().st_size,
    }


def _check_archive_entry(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or name.startswith("/") or "\\" in name or ":" in name:
        raise ValueError(f"产物包包含非法路径：{name!r}")
    parts = PurePosixPath(name).parts
    if any(part in ("..", "") for part in parts):
        raise ValueError(f"产物包包含越界路径：{name!r}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise ValueError(f"产物包不允许包含符号链接：{name!r}")


def _read_zip_member(
    archive: zipfile.ZipFile,
    name: str,
    budget: list[int],
    *,
    limit: int | None = None,
) -> bytes:
    """安全读取单个 zip 成员并实时扣减解压总字节预算，防止伪造头部的解压炸弹。"""
    chunks: list[bytes] = []
    size = 0
    with archive.open(name, "r") as handle:
        while True:
            block = handle.read(256 * 1024)
            if not block:
                break
            size += len(block)
            budget[0] -= len(block)
            if limit is not None and size > limit:
                raise ValueError(f"产物包成员过大：{name}")
            if budget[0] < 0:
                raise ValueError("产物包解压总大小超过安全上限（1 GB）")
            chunks.append(block)
    return b"".join(chunks)


def _match_course(
    manager: JobManager,
    owner: str,
    root: Path,
    course_meta: dict[str, Any],
    fallback_title: str,
) -> dict[str, Any]:
    title = str(course_meta.get("course") or course_meta.get("title") or fallback_title or "导入课程").strip()
    incoming_source = str(course_meta.get("source") or "").strip()
    with manager.locked():
        owned = [item for item in manager.courses.values() if item.get("owner") == owner and not item.get("deleted_at")]
        # 优先匹配同公开来源且同名课程；其次匹配同根目录同名课程
        for candidate in owned:
            cand_source = str(candidate.get("source") or "")
            cand_public = public_source(cand_source) if cand_source else ""
            if incoming_source and cand_public == incoming_source and str(candidate.get("title") or "") == title:
                return dict(candidate)
        for candidate in owned:
            if (
                str(candidate.get("title") or "") == title
                and Path(str(candidate.get("out_dir") or "")).resolve() == root
            ):
                return dict(candidate)

        base_id = hashlib.sha256(f"{owner}\n{root}\nimported:{title}".encode()).hexdigest()[:20]
        course_id = base_id
        existing = manager.courses.get(course_id)
        if existing is not None and existing.get("owner") != owner:
            course_id = uuid.uuid4().hex[:20]

        sessions: list[dict[str, Any]] = []
        course_dir = safe_name(title, "未分类")
        for item in course_meta.get("sessions") or []:
            if not isinstance(item, dict):
                continue
            folder_name = safe_name(
                str(item.get("folder_name") or item.get("date") or item.get("title") or item.get("id") or "session"),
                "session",
            )
            sessions.append(
                {
                    "id": str(item.get("id") or ""),
                    "date": str(item.get("date") or ""),
                    "title": str(item.get("title") or ""),
                    "views": item.get("views"),
                    "week": item.get("week"),
                    "course": str(item.get("course") or title),
                    "meta": dict(item.get("meta") or {}),
                    "folder": str(root / course_dir / folder_name),
                }
            )
        return {
            "id": course_id,
            "owner": owner,
            "title": title,
            "course": title,
            "source": incoming_source,
            "out_dir": str(root),
            "resolver": str(course_meta.get("resolver") or ""),
            "resolver_label": str(course_meta.get("resolver_label") or ""),
            "view_options": list(course_meta.get("view_options") or []),
            "term_start": str(course_meta.get("term_start") or ""),
            "term_source": str(course_meta.get("term_source") or ""),
            "sessions": sessions,
            "updated_at": time.time(),
        }


def _find_conflicting_job(
    manager: JobManager,
    owner: str,
    course_id: str,
    session_id: str,
    target_dir: Path,
) -> Job | None:
    resolved_target = target_dir.resolve()
    with manager.locked():
        for job in manager.jobs.values():
            if job.owner != owner or job.deleted_at is not None:
                continue
            if job.out_dir.resolve() == resolved_target:
                return job
            if session_id and job.course_id == course_id and job.session_id == session_id:
                return job
    return None


def _allocate_copy_dir(manager: JobManager, parent: Path, base_name: str, root: Path) -> Path:
    with manager.locked():
        occupied = {job.out_dir.resolve() for job in manager.jobs.values()}
    for index in range(2, 1000):
        candidate = (parent / f"{base_name}-{index}").resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("生成的副本目录超出输出根目录")
        if not candidate.exists() and candidate not in occupied:
            return candidate
    raise ValueError("无法为副本分配唯一目录名")


def _rename_dir(src: Path, dst: Path) -> None:
    """移动目录；Windows 上新建目录刚写完文件时偶发短暂句柄占用（WinError 5），短暂重试。"""
    attempts = 6 if sys.platform == "win32" else 1
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


def _extract_job_files(  # noqa: PLR0912  历史遗留的长函数，拆分后移除
    archive: zipfile.ZipFile,
    key: str,
    file_records: list[dict[str, Any]],
    target_dir: Path,
    budget: list[int],
    *,
    replace_existing: bool,
) -> None:
    prefix = f"jobs/{key}/files/"
    expected_hashes = {
        str(item["rel"]): str(item.get("sha256") or "")
        for item in file_records
        if isinstance(item, dict) and item.get("rel")
    }
    members = [info for info in archive.infolist() if not info.is_dir() and info.filename.startswith(prefix)]
    if not members:
        raise ValueError(f"产物包中缺少课次文件（{key}）")

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=target_dir.parent, prefix=".c2m-import-"))
    backup: Path | None = None
    try:
        seen_rels: set[str] = set()
        for info in members:
            rel = info.filename[len(prefix) :]
            dest = resolve_relative(staging, rel)
            if not dest.resolve().is_relative_to(staging.resolve()):
                raise ValueError(f"非法的任务内路径：{rel!r}")
            raw = _read_zip_member(archive, info.filename, budget)
            expected = expected_hashes.get(rel)
            if expected and hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"产物包文件校验失败（内容可能已损坏）：{rel}")
            if rel == "job.json":
                try:
                    manifest_data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    manifest_data = None
                if isinstance(manifest_data, dict):
                    rewritten = from_job_refs(manifest_data, target_dir)
                    raw = (json.dumps(rewritten, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            seen_rels.add(rel)

        missing = set(expected_hashes) - seen_rels
        if missing:
            raise ValueError(f"产物包缺少清单声明的文件：{sorted(missing)[0]}")

        if target_dir.exists():
            if not replace_existing:
                raise ValueError(f"目标目录已存在：{target_dir}")
            backup = target_dir.with_name(f".c2m-backup-{uuid.uuid4().hex[:8]}")
            _rename_dir(target_dir, backup)
        _rename_dir(staging, target_dir)
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if backup is not None and backup.exists() and not target_dir.exists():
            try:
                _rename_dir(backup, target_dir)
            except OSError:
                pass
        raise


def _read_stage_cache(
    archive: zipfile.ZipFile,
    key: str,
    raw: Any,
    names: set[str],
    budget: list[int],
) -> list[tuple[dict[str, Any], Any]]:
    """读出并校验包内某课次的阶段缓存。清单与文件不符属于损坏的产物包，整体拒绝；没有缓存时返回空列表。"""
    if not isinstance(raw, list):
        return []
    prefix = f"jobs/{key}/stages/"
    items: list[tuple[dict[str, Any], Any]] = []
    for meta in raw:
        if not isinstance(meta, dict):
            continue
        member = str(meta.get("file") or "")
        if not member.startswith(prefix) or member not in names:
            raise ValueError(f"产物包缺少清单声明的阶段缓存：{member or meta.get('stage')}")
        payload = _read_zip_member(archive, member, budget, limit=MAX_MANIFEST_BYTES)
        expected = str(meta.get("sha256") or "")
        if expected and hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError(f"产物包阶段缓存校验失败（内容可能已损坏）：{member}")
        try:
            items.append((meta, json.loads(payload.decode("utf-8"))))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"产物包阶段缓存无法解析：{member}") from exc
    return items


def _sanitize_imported_options(
    raw_options: dict[str, Any] | None,
    target_dir: Path,
    *,
    server_mode: bool,
    is_admin: bool,
):
    if not isinstance(raw_options, dict):
        return None
    try:
        resolved = from_job_refs(raw_options, target_dir)
        options = options_from_dict(resolved)
    except (ValueError, TypeError, KeyError):
        return None
    llm_cfg = replace(options.llm, api_key="")
    agent_cfg = options.agent
    if server_mode and not is_admin:
        # 服务器普通用户不允许通过导入产物包注入自定义命令或任意模型接口
        llm_cfg = replace(llm_cfg, base_url=LlmConfig.from_env().base_url)
        if agent_cfg.command:
            agent_cfg = AgentConfig()
    return replace(options, out_dir=target_dir, llm=llm_cfg, agent=agent_cfg)


def _resolve_result(raw_result: dict[str, Any] | None, target_dir: Path, timings: dict[str, float]) -> JobResult:
    resolved_dir = target_dir.resolve()
    resolved_paths: dict[str, Path | None] = {
        "notes": None,
        "detailed_notes": None,
        "brief_notes": None,
        "note_workspace": None,
    }
    if isinstance(raw_result, dict):
        mapped = from_job_refs(raw_result, target_dir)
        for field_name in resolved_paths:
            value = mapped.get(field_name)
            if not value:
                continue
            candidate = Path(str(value))
            try:
                cand_resolved = candidate.resolve()
            except OSError:
                continue
            if not cand_resolved.is_relative_to(resolved_dir):
                continue
            if field_name == "note_workspace":
                if cand_resolved.is_dir():
                    resolved_paths[field_name] = cand_resolved
            elif cand_resolved.is_file():
                resolved_paths[field_name] = cand_resolved
    return JobResult(
        output_dir=resolved_dir,
        timings=dict(timings),
        notes=resolved_paths["notes"],
        detailed_notes=resolved_paths["detailed_notes"],
        brief_notes=resolved_paths["brief_notes"],
        note_workspace=resolved_paths["note_workspace"],
    )


def _merge_course_session(course: dict[str, Any], job: Job) -> None:
    sessions: list[dict[str, Any]] = list(course.get("sessions") or [])
    folder_str = str(job.out_dir)
    for item in sessions:
        if (job.session_id and item.get("id") == job.session_id) or item.get("folder") == folder_str:
            item["folder"] = folder_str
            if not item.get("title"):
                item["title"] = job.title
            if not item.get("date") and job.session_date:
                item["date"] = job.session_date
            if item.get("week") is None and job.week is not None:
                item["week"] = job.week
            course["sessions"] = sessions
            return
    sessions.append(
        {
            "id": job.session_id or job.id,
            "date": job.session_date,
            "title": job.title,
            "views": None,
            "week": job.week,
            "course": job.course or course.get("title") or "",
            "meta": {},
            "folder": folder_str,
        }
    )
    course["sessions"] = sessions


def import_bundle(  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
    manager: JobManager,
    bundle_path: Path,
    *,
    owner: str = "local",
    root: Path,
    conflict: str = "skip",
    server_mode: bool = False,
    is_admin: bool = True,
    origin_label: str = "产物包",
) -> dict[str, Any]:
    """校验并导入 .c2m.zip 课程产物包到目标 Hub。"""
    if conflict not in CONFLICT_MODES:
        raise ValueError(f"不支持的冲突处理策略：{conflict!r}，可选 {', '.join(CONFLICT_MODES)}")
    bundle_path = Path(bundle_path)
    if not bundle_path.is_file():
        raise ValueError("产物包文件不存在")
    if bundle_path.stat().st_size > MAX_BUNDLE_BYTES:
        raise ValueError(f"产物包大小超过上限（{MAX_BUNDLE_BYTES // (1024 * 1024)} MB）")

    root = Path(root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    budget = [MAX_UNCOMPRESSED_BYTES]

    try:
        archive = zipfile.ZipFile(bundle_path, "r")
    except zipfile.BadZipFile as exc:
        raise ValueError("上传的文件不是有效的 course2md 产物包（ZIP 格式无效）") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_BUNDLE_FILES + 50:
            raise ValueError("产物包内文件数超过安全上限")
        declared_total = 0
        for info in infos:
            _check_archive_entry(info)
            declared_total += info.file_size
            if declared_total > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("产物包解压总大小超过安全上限（1 GB）")
        names = {info.filename for info in infos}
        if "bundle.json" not in names:
            raise ValueError("产物包缺少 bundle.json 清单，不是有效的 course2md 产物包")
        try:
            manifest = json.loads(
                _read_zip_member(archive, "bundle.json", budget, limit=MAX_MANIFEST_BYTES).decode("utf-8")
            )
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("产物包清单 bundle.json 无法解析") from exc
        if not isinstance(manifest, dict) or manifest.get("format") != BUNDLE_FORMAT:
            raise ValueError("不支持的产物包格式")
        if manifest.get("version") != BUNDLE_VERSION:
            raise ValueError(f"不支持的产物包版本：{manifest.get('version')!r}（当前仅支持 {BUNDLE_VERSION}）")
        raw_jobs = manifest.get("jobs")
        if not isinstance(raw_jobs, list) or not raw_jobs:
            raise ValueError("产物包中没有可导入的课次")

        raw_course = manifest.get("course")
        course_meta: dict[str, Any] = raw_course if isinstance(raw_course, dict) else {}
        first_title = str(raw_jobs[0].get("course") or raw_jobs[0].get("title") or "导入课程")
        course = _match_course(manager, owner, root, course_meta, first_title)
        existed_before = course["id"] in manager.courses

        imported_jobs: list[dict[str, Any]] = []
        overwritten_jobs: list[dict[str, Any]] = []
        skipped_jobs: list[dict[str, Any]] = []
        warnings: list[str] = []

        for entry in raw_jobs:
            if not isinstance(entry, dict) or not entry.get("key"):
                raise ValueError("产物包课次清单格式无效")
            key = str(entry["key"])
            title = str(entry.get("title") or "未命名课次")
            job_course = str(entry.get("course") or course.get("course") or course.get("title") or "")
            session_id = str(entry.get("session_id") or "")
            folder_name = safe_name(
                str(entry.get("folder_name") or entry.get("session_date") or title or key),
                "session",
            )
            course_dir = safe_name(job_course or course.get("title") or "未分类", "未分类")
            target_dir = (root / course_dir / folder_name).resolve()
            if not target_dir.is_relative_to(root):
                raise ValueError("目标课次路径超出输出根目录")

            existing = _find_conflicting_job(manager, owner, course["id"], session_id, target_dir)
            dir_non_empty = target_dir.is_dir() and any(target_dir.iterdir())
            is_conflict = existing is not None or dir_non_empty
            overwrite_this = False

            if is_conflict:
                if conflict == "skip":
                    skipped_jobs.append(
                        {"title": title, "session_id": session_id, "reason": "目标 Hub 已存在同名课次，已跳过"}
                    )
                    continue
                if conflict == "overwrite":
                    if existing is not None and (existing.status in ACTIVE_STATUSES or existing.pending_messages):
                        skipped_jobs.append(
                            {"title": title, "session_id": session_id, "reason": "目标课次正在处理中，无法覆盖"}
                        )
                        continue
                    if existing is not None:
                        target_dir = existing.out_dir.resolve()
                        if not target_dir.is_relative_to(root):
                            target_dir = (root / course_dir / folder_name).resolve()
                    overwrite_this = True
                elif conflict == "copy":
                    target_dir = _allocate_copy_dir(manager, target_dir.parent, folder_name, root)
                    existing = None

            stage_items = _read_stage_cache(archive, key, entry.get("stage_cache"), names, budget)
            _extract_job_files(
                archive,
                key,
                list(entry.get("files") or []),
                target_dir,
                budget,
                replace_existing=overwrite_this,
            )

            job_id = existing.id if (overwrite_this and existing is not None) else uuid.uuid4().hex[:12]
            raw_timings = entry.get("timings")
            timings = {str(k): float(v) for k, v in raw_timings.items()} if isinstance(raw_timings, dict) else {}
            status = str(entry.get("status") or "done")
            if status in ACTIVE_STATUSES:
                status = "done"
            options = _sanitize_imported_options(
                entry.get("options"),
                target_dir,
                server_mode=server_mode,
                is_admin=is_admin,
            )
            result = _resolve_result(entry.get("result"), target_dir, timings)
            job = Job(
                id=job_id,
                source=str(entry.get("source") or ""),
                title=title,
                out_dir=target_dir,
                course=job_course,
                course_id=course["id"],
                session_id=session_id if not (conflict == "copy" and is_conflict) else "",
                session_date=str(entry.get("session_date") or ""),
                week=entry.get("week") if isinstance(entry.get("week"), int) else None,
                status=status,
                stage=str(entry.get("stage") or "完成"),
                progress=float(1.0 if entry.get("progress") is None else entry["progress"]),
                message=str(entry.get("message") or f"已从{origin_label}导入"),
                error=str(entry.get("error") or ""),
                started_at=float(entry.get("started_at") or time.time()),
                agent=str(entry.get("agent") or ""),
                agent_model=str(entry.get("agent_model") or ""),
                owner=owner,
                timings=timings,
                messages=_sanitize_messages(list(entry.get("messages") or [])),
                revision=max(1, int(entry.get("revision") or 1)),
                options=options,
                result=result,
            )
            job.files = collect_files(result)
            job.log(f"已从{origin_label}导入")

            with manager.locked():
                manager.save_job(job, replace_existing=True)
                manager.jobs[job.id] = job

            if stage_items:
                _, cache_warnings = restore_stage_cache(manager, job, stage_items, replace=overwrite_this)
                warnings.extend(f"「{title}」{message}" for message in cache_warnings)

            versions = getattr(manager, "versions", None)
            if versions is not None:
                if overwrite_this:
                    versions.discard(job.id)
                raw_versions = entry.get("versions") or []
                restored_versions = 0
                if isinstance(raw_versions, list):
                    for ver in sorted(
                        (item for item in raw_versions if isinstance(item, dict)),
                        key=lambda item: int(item.get("version") or 0),
                    ):
                        detailed_arc = str(ver.get("detailed_file") or "")
                        if not detailed_arc or detailed_arc not in names:
                            continue
                        detailed_bytes = _read_zip_member(archive, detailed_arc, budget)
                        brief_arc = str(ver.get("brief_file") or "")
                        brief_bytes = (
                            _read_zip_member(archive, brief_arc, budget) if brief_arc and brief_arc in names else None
                        )
                        versions.import_version(
                            job.id,
                            detailed_bytes,
                            brief_bytes,
                            str(ver.get("label") or "导入"),
                            str(ver.get("model") or ""),
                            float(ver["created_at"]) if ver.get("created_at") is not None else None,
                        )
                        restored_versions += 1
                if restored_versions == 0 and result.detailed_notes and result.detailed_notes.is_file():
                    manager.record_version(job, f"从{origin_label}导入")

            _merge_course_session(course, job)
            snap = job.snapshot(0)
            if overwrite_this:
                overwritten_jobs.append(snap)
            else:
                imported_jobs.append(snap)

        if imported_jobs or overwritten_jobs:
            course["updated_at"] = time.time()
            record = cast(CourseRecord, course)
            with manager.locked():
                manager.store.save_course(record)
                manager.courses[record["id"]] = record
            course_payload = manager.course_snapshot(record)
        elif existed_before:
            course_payload = manager.course_snapshot(manager.courses[course["id"]])
        else:
            course_payload = None

        return {
            "course": course_payload,
            "imported": imported_jobs,
            "overwritten": overwritten_jobs,
            "skipped": skipped_jobs,
            "warnings": warnings,
        }

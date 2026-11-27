"""课次的目录名、已有结果检查与解析结果描述。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import (
    default_term_start,
)
from ..resolvers import (
    parse_date,
    resolve_term_start,
    view_options,
    week_of,
)
from ..util.text import safe_name
from .records import ExistingResult, ResolvedCourse, SessionRecord

_TITLE_DATE = re.compile(r"[ _\-]?(\d{4})[-/.年](\d{1,2})(?:[-/.月](\d{1,2}))?")
_TIME_PART = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})")


def session_folder(session, course: str = "") -> str:
    """课次在课程文件夹下的名字：优先「日期 时-分」，其次标题。"""
    begin = session.meta.get("begin") or session.date or ""
    found = _TIME_PART.search(begin)
    if found:
        return f"{found[1]} {found[2]}-{found[3]}"
    if session.date:
        return session.date
    title = session.title
    if course and title.startswith(course):
        title = title[len(course) :].strip()
    return safe_name(title or session.session_id, session.session_id or "session")


def existing_result(folder: Path) -> ExistingResult | None:
    """检查课次目录里是否已有上一次的结果（完整或中断）。"""
    try:
        if not folder.is_dir():
            return None
        entries = list(folder.iterdir())
    except OSError:
        return None
    if not any(entry.is_file() for entry in entries):
        return None
    notes: list[str] = []
    updated = 0.0
    note_dir = folder / "notes"
    note_entries = [*entries, *note_dir.glob("*.md")]
    for path in note_entries:
        try:
            updated = max(updated, path.stat().st_mtime)
        except OSError:
            continue
        if path.is_file() and path.name.endswith((".notes.md", ".brief.md")):
            notes.append(path.relative_to(folder).as_posix())
    complete = False
    manifest = folder / "job.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        detailed = data.get("detailed_notes")
        complete = bool(notes) and bool(detailed) and Path(detailed).is_file()
    return {
        "path": str(folder),
        "complete": complete,
        "file_count": len(entries),
        "updated_at": round(updated, 3),
        "notes": sorted(notes),
    }


def resolver_fields(resolver, source: str) -> dict[str, Any]:
    """解析器提供给界面的信息：名称、显示名与可选机位。"""
    return {
        "resolver": resolver.name,
        "resolver_label": getattr(resolver, "label", "") or resolver.name,
        "view_options": [
            {"id": option.id, "label": option.label, "default": option.default}
            for option in view_options(resolver, source)
        ],
    }


def describe_sessions(sessions, root: Path, configured: str = "") -> ResolvedCourse:
    hint = next((session.meta.get("term_start") for session in sessions if session.meta.get("term_start")), "")
    term_start, basis = resolve_term_start(
        configured or default_term_start(), hint, [session.date for session in sessions]
    )
    payload: list[SessionRecord] = []
    for session in sessions:
        day = parse_date(session.date)
        week = week_of(day, term_start) if day and term_start else None
        folder = (
            root / safe_name(session.course, "未分类") / safe_name(session_folder(session, session.course), "session")
        )
        payload.append(
            {
                "id": session.session_id,
                "date": session.date,
                "title": session.title,
                "views": session.views,
                "week": week,
                "course": session.course,
                "meta": session.meta,
                "folder": str(folder),
            }
        )
    return {
        "course": sessions[0].course if sessions else "",
        "sessions": payload,
        "term_start": term_start.isoformat() if term_start else "",
        "term_source": basis,
    }

"""课程与课次记录：TypedDict 只描述结构，运行时仍是普通字典，旧数据与新记录互通。"""

import json

from course2md.jobs.manager import JobManager
from course2md.jobs.records import CourseRecord, SessionRecord


def test_course_round_trips_through_store_with_unknown_resolver_fields(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    try:
        session: SessionRecord = {
            "id": "s1",
            "date": "2026-03-01",
            "title": "第一讲",
            "views": 2,
            "week": 1,
            "course": "线性代数",
            "meta": {},
            "folder": str(tmp_path / "线性代数" / "2026-03-01"),
        }
        resolved = {
            "course": "线性代数",
            "sessions": [session],
            "term_start": "2026-02-23",
            "term_source": "配置",
            "resolver": "demo",
            "extra_from_plugin": {"keep": True},  # 解析器附带的未知字段必须被原样保留
        }
        course = manager.remember_course("https://example.test/c", tmp_path, resolved)
    finally:
        manager.close()

    reopened = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    try:
        loaded: CourseRecord = reopened.courses[course["id"]]
        assert loaded["title"] == "线性代数"
        assert loaded["owner"] == "local"
        assert loaded.get("extra_from_plugin") == {"keep": True}  # type: ignore[misc]
        assert loaded.get("sessions") == [session]
        snapshot = reopened.course_snapshot(loaded)
        assert snapshot["sessions"][0]["job_id"] == ""
        assert snapshot["sessions"][0]["existing"] is None
        json.dumps(snapshot, ensure_ascii=False)  # 记录必须能直接序列化给前端
    finally:
        reopened.close()


def test_reparse_without_sessions_keeps_saved_sessions(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    try:
        session = {
            "id": "s1",
            "date": "",
            "title": "t",
            "views": 1,
            "week": None,
            "course": "c",
            "meta": {},
            "folder": "x",
        }
        first = manager.remember_course(
            "src", tmp_path, {"course": "c", "sessions": [session], "term_start": "2026-01-01"}
        )
        again = manager.remember_course("src", tmp_path, {"course": "c", "sessions": []})
        assert again["id"] == first["id"]
        assert again.get("sessions") == [session]
        assert again.get("term_start") == "2026-01-01"
    finally:
        manager.close()

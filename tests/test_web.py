"""通过 HTTP 检查配置传递、产物访问和网页资源。"""

from __future__ import annotations

import http.client
import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from course2md.hub.app import create_app
from course2md.jobs import manager as server
from course2md.resolvers import ResolverError
from live_server import LiveServer
from resolver_patch import patch_get_resolver


def _idle_manager(root):
    manager = server.JobManager.__new__(server.JobManager)
    manager.jobs, manager.courses, manager.warnings = {}, {}, []
    manager._queue = queue.Queue()
    manager._lock = threading.RLock()
    manager.store = server.WorkspaceStore(root / ".course2md" / "workspace.sqlite3")
    return manager


@pytest.fixture
def web(tmp_path, monkeypatch):

    monkeypatch.setattr("course2md.agents.dsh.dsh_prefix", lambda: ["fake-dsh"])
    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: ["fake-codex"])
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(output))
    outside = tmp_path / "output-other"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-output", encoding="utf-8")
    captured = []
    job = server.Job(id="probe", source="local", title="probe", out_dir=output)
    manager = _idle_manager(output)
    manager.jobs[job.id] = job

    def submit(job, options):
        captured.append(options)
        return server.JobManager.submit(manager, job, options)

    manager.submit = submit

    httpd = LiveServer(create_app(manager))
    port = httpd.port

    def request(method, path, body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            data = raw if raw is not None else (None if body is None else json.dumps(body))
            connection.request(method, path, body=data, headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, response.headers, response.read()
        finally:
            connection.close()

    try:
        yield SimpleNamespace(request=request, captured=captured, output=output, job=job, manager=manager, port=port)
    finally:
        httpd.stop()


def test_job_keeps_explicit_flags_and_environment_runtime_settings(web, monkeypatch):
    monkeypatch.setenv("COURSE2MD_AGENT_TIMEOUT", "420")
    monkeypatch.setenv("COURSE2MD_CHUNK_CHARS", "4321")
    monkeypatch.setenv("COURSE2MD_BEAM_SIZE", "7")
    monkeypatch.setenv("COURSE2MD_FRAME_INTERVAL", "3.5")
    monkeypatch.setenv("COURSE2MD_FRAME_SELECTOR", "general")
    status, headers, body = web.request(
        "POST",
        "/api/v1/jobs",
        {
            "source": "lecture.mp4",
            "agent": "dsh",
            "detail": False,
            "frames": False,
            "device": "cpu",
            "model": "explicit-model",
            "llm_model": "explicit-llm",
        },
    )
    assert status == 200
    options = web.captured[0]
    assert (options.detail, options.frames) == (False, False)
    assert options.agent.timeout == 420
    assert options.agent.preset == "dsh"
    assert options.frame_interval == 3.5
    assert options.frame_selector == "general"
    assert options.llm.chunk_chars == 4321
    assert options.llm.model == "explicit-llm"
    assert options.asr.model == "explicit-model"
    assert options.asr.beam_size == 7
    assert options.asr.compute_type == "int8"


def test_config_and_omitted_job_flags_use_environment_defaults(web, monkeypatch):
    monkeypatch.setenv("COURSE2MD_AGENT", "none")
    monkeypatch.setenv("COURSE2MD_DETAIL", "0")
    monkeypatch.setenv("COURSE2MD_FRAMES", "0")
    monkeypatch.setenv("COURSE2MD_WHISPER_MODEL", "environment-model")
    status, headers, body = web.request("GET", "/api/v1/config")
    assert status == 200
    config = json.loads(body)
    assert (config["detail"], config["frames"]) == (False, False)
    assert config["model"] == "environment-model"
    assert config["agent_default"] == ""
    status, headers, body = web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4"})
    assert status == 200
    options = web.captured[0]
    assert (options.detail, options.frames) == (False, False)
    assert options.agent.resolved_command() == []


@pytest.mark.parametrize("route", ["file", "preview"])
@pytest.mark.parametrize("relative", ["../output-other/secret.txt", "..\\output-other\\secret.txt"])
def test_file_routes_reject_same_prefix_sibling_directory(web, route, relative):
    status, headers, body = web.request("GET", f"/api/v1/jobs/probe/{route}?rel={quote(relative)}")
    assert status == 404
    assert b"outside-output" not in body


def test_file_routes_serve_nested_unicode_filename(web):
    path = web.output / "notes" / "课堂笔记.md"
    path.parent.mkdir()
    path.write_text("# 课堂笔记\n", encoding="utf-8")
    relative = quote("notes/课堂笔记.md")
    status, headers, body = web.request("GET", f"/api/v1/jobs/probe/preview?rel={relative}")
    assert status == 200
    assert json.loads(body)["text"] == "# 课堂笔记\n"
    status, headers, body = web.request("GET", f"/api/v1/jobs/probe/file?rel={relative}")
    assert status == 200
    assert body == path.read_bytes()
    assert headers["Content-Disposition"] == f"attachment; filename*=UTF-8''{quote(path.name)}"


def test_markdown_preview_renders_safe_local_images_and_companion_link(web):
    notes = web.output / "notes"
    assets = notes / "课堂.assets"
    assets.mkdir(parents=True)
    image = assets / "画面 1.jpg"
    image.write_bytes(b"image-fixture")
    (notes / "课堂.notes.md").write_text("# 详细版\n", encoding="utf-8")
    brief = notes / "课堂.brief.md"
    brief.write_text(
        "# 缩略版\n\n![课堂](课堂.assets/画面%201.jpg)\n\n"
        "[详细版](课堂.notes.md)\n\n<script>alert(1)</script>\n\n"
        "![外部](https://example.com/private.jpg)\n\n"
        "![越界](../../output-other/secret.txt)\n",
        encoding="utf-8",
    )
    status, headers, body = web.request("GET", "/api/v1/jobs/probe/preview?rel=" + quote("notes/课堂.brief.md"))
    assert status == 200
    html = json.loads(body)["html"]
    assert "<h1>缩略版</h1>" in html
    assert 'data-note-rel="notes/课堂.notes.md"' in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert 'src="https://' not in html and 'src="../../' not in html
    assert "&amp;inline=1" in html
    status, headers, body = web.request(
        "GET", "/api/v1/jobs/probe/file?rel=" + quote("notes/课堂.assets/画面 1.jpg") + "&inline=1"
    )
    assert status == 200 and body == b"image-fixture"
    assert headers["Content-Disposition"].startswith("inline;")
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_file_listing_keeps_both_notes_bundle_and_images(web):
    from course2md.pipeline import JobResult

    detailed = web.output / "lecture.notes.md"
    brief = web.output / "lecture.brief.md"
    for name in (detailed.name, brief.name, "lecture.notes.zip", "slide.jpg", "lecture.notes.md.lock"):
        (web.output / name).write_bytes(b"fixture")
    work = web.output / "agent-workspace"
    work.mkdir()
    (work / "agent.log").write_text("internal", encoding="utf-8")
    files = server._collect_files(
        JobResult(output_dir=web.output, notes=brief, detailed_notes=detailed, brief_notes=brief)
    )
    assert [item["kind"] for item in files] == ["notes", "notes", "bundle", "image"]
    assert {item["name"] for item in files[:2]} == {detailed.name, brief.name}


def test_web_lists_and_selects_codex(web, monkeypatch):
    monkeypatch.setenv("COURSE2MD_AGENT", "codex")
    status, headers, body = web.request("GET", "/api/v1/config")
    assert status == 200
    config = json.loads(body)
    assert set(config["agents"]) == {"dsh", "codex"}
    assert config["agent_default"] == "codex"
    status, headers, body = web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4", "agent": "codex"})
    assert status == 200
    assert web.captured[0].agent.preset == "codex"
    assert json.loads(body)["job"]["agent"] == "codex"


def test_web_cleanup_defaults_and_message_request_are_explicit(web, monkeypatch):
    monkeypatch.setenv("COURSE2MD_DELETE_VIDEO", "1")
    status, headers, body = web.request("GET", "/api/v1/config")
    assert json.loads(body)["delete_video"] is True
    web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4", "delete_video": False})
    assert web.captured[0].delete_video is False
    web.manager.send = Mock()
    status, headers, body = web.request("POST", "/api/v1/jobs/probe/messages", {"text": "补充例题", "id": "one"})
    assert status == 200
    web.manager.send.assert_called_once_with(web.job, "补充例题", "one")
    status, headers, body = web.request("POST", "/api/v1/jobs/probe/messages", {"text": ""})
    assert status == 400


def test_output_cursor_survives_buffer_truncation(web):
    for index in range(530):
        web.job.emit({"kind": "assistant", "text": f"输出 {index}"})
        web.job.log(f"进度 {index}")
    status, headers, body = web.request("GET", "/api/v1/jobs/probe?after=525&since=525")
    data = json.loads(body)
    assert [event["seq"] for event in data["events"]] == [526, 527, 528, 529, 530]
    assert data["event_offset"] == data["log_offset"] == 530
    assert len(data["logs"]) == 5


def test_followup_queue_is_idempotent_and_survives_restart(tmp_path, monkeypatch):
    from course2md.config import LlmConfig
    from course2md.pipeline import JobResult
    from course2md.refine import conversation
    from course2md.stages.options import JobOptions

    root = tmp_path / "output"
    work = root / "notes" / "agent-workspace" / "one"
    work.mkdir(parents=True)
    detailed, brief = root / "课.notes.md", root / "课.brief.md"
    detailed.write_text("# 详细版\n", encoding="utf-8")
    brief.write_text("# 缩略版\n", encoding="utf-8")
    (work / "lesson.json").write_text("{}", encoding="utf-8")
    options = JobOptions(source="lecture.mp4", out_dir=root, llm=LlmConfig(api_key="secret-for-test"))
    result = JobResult(output_dir=root, notes=detailed, detailed_notes=detailed, brief_notes=brief, note_workspace=work)
    job = server.Job(
        id="course-one",
        source=options.source,
        title="课程",
        out_dir=root,
        status="done",
        agent="builtin",
        options=options,
        result=result,
        revision=1,
    )
    manager = _idle_manager(root)
    manager.send(job, "补充例题", "message-one")
    manager.send(job, "补充例题", "message-one")
    assert manager._queue.qsize() == job.pending_messages == 1
    monkeypatch.setattr(conversation, "continue_notes", lambda *args, **kwargs: conversation.Reply("已补充。", True))
    queued_job, request = manager._queue.get_nowait()
    manager._chat(queued_job, request)
    assert job.status == "done" and job.revision == 2
    assert job.messages[-1]["text"] == "已补充。"
    saved = manager.store.load_jobs()
    assert "secret-for-test" not in json.dumps(saved, ensure_ascii=False)
    restored = _idle_manager(root)
    recovered = restored._restore_job(next(item for item in saved if item["id"] == "course-one"))
    assert recovered.snapshot()["can_chat"]
    assert recovered.messages[-1]["text"] == "已补充。"
    assert recovered.revision == 2


def test_manager_shutdown_releases_queue_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(tmp_path))
    manager = server.JobManager()
    manager.close()
    assert not manager._worker.is_alive()


def test_web_rejects_unavailable_codex_before_enqueuing(web, monkeypatch):

    monkeypatch.setattr("course2md.agents.codex.codex_prefix", lambda: None)
    status, headers, body = web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4", "agent": "codex"})
    assert status == 400
    assert "找不到 codex" in json.loads(body)["error"]
    assert web.captured == []


def test_completed_job_exposes_backend_and_organizing_duration(tmp_path, monkeypatch):
    from course2md.pipeline import JobResult
    from course2md.stages.options import JobOptions

    job = server.Job(id="timed", source="fixture", title="lecture", out_dir=tmp_path, agent="codex")
    options = JobOptions(source="fixture", out_dir=tmp_path)
    result = JobResult(output_dir=tmp_path, timings={"整理": 4.125})
    manager = _idle_manager(tmp_path)
    manager._queue = SimpleNamespace(get=Mock(side_effect=[(job, options), StopIteration]))
    monkeypatch.setattr(server, "run_job", lambda *args, **kwargs: result)
    with pytest.raises(StopIteration):
        manager._run()
    snapshot = job.snapshot()
    assert snapshot["status"] == "done"
    assert snapshot["agent"] == "codex"
    assert snapshot["timings"] == {"整理": 4.125}
    assert "整理耗时 4.1 秒" in snapshot["message"]


def _demo_sessions():
    from course2md.resolvers import SessionInfo

    return [
        SessionInfo(
            session_id="3",
            date="2026-11-09",
            title="编译原理 2026-11-09 15:15",
            views=1,
            course="编译原理",
            meta={"begin": "2026-11-09 15:15:00", "subject": "编译原理"},
        ),
        SessionInfo(
            session_id="2",
            date="2026-11-02",
            title="编译原理 2026-11-02 13:30",
            views=1,
            course="编译原理",
            meta={"begin": "2026-11-02 13:30:00", "subject": "编译原理"},
        ),
    ]


def _fake_resolver():
    return SimpleNamespace(name="demo", list_sessions=lambda source: _demo_sessions())


def _write_result(folder: Path, *, complete: bool) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "旧.srt").write_text("1\n", encoding="utf-8")
    if complete:
        note = folder / "旧.notes.md"
        note.write_text("# 旧笔记\n", encoding="utf-8")
        (folder / "job.json").write_text(json.dumps({"detailed_notes": str(note)}), encoding="utf-8")


def test_resolve_reports_course_week_and_existing_results(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    _write_result(web.output / "编译原理" / "2026-11-02 13-30", complete=True)
    _write_result(web.output / "编译原理" / "2026-11-09 15-15", complete=False)

    status, headers, body = web.request(
        "POST", "/api/v1/resolve", {"source": "demo://course", "out_dir": str(web.output)}
    )
    assert status == 200
    data = json.loads(body)
    assert data["resolver"] == "demo"
    assert data["course"] == "编译原理"
    assert data["term_start"] == "2026-11-02"
    assert data["term_source"] == "inferred"
    assert [session["week"] for session in data["sessions"]] == [2, 1]
    assert data["sessions"][0]["existing"]["complete"] is False
    assert data["sessions"][1]["existing"]["complete"] is True
    assert data["sessions"][0]["folder"].endswith("2026-11-09 15-15")
    assert Path(data["sessions"][0]["folder"]).parent.name == "编译原理"


def test_resolve_honours_explicit_term_start(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    status, headers, body = web.request(
        "POST",
        "/api/v1/resolve",
        {
            "source": "demo://course",
            "out_dir": str(web.output),
            "term_start": "2026-09-07",
        },
    )
    assert status == 200
    data = json.loads(body)
    assert data["term_start"] == "2026-09-07"
    assert data["term_source"] == "configured"
    assert [session["week"] for session in data["sessions"]] == [10, 9]


def test_batch_create_needs_overwrite_confirmation(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    _write_result(web.output / "编译原理" / "2026-11-02 13-30", complete=True)

    payload = {
        "source": "demo://course",
        "session_ids": ["3", "2"],
        "agent": "none",
        "out_dir": str(web.output),
        "term_start": "2026-11-02",
    }
    status, headers, body = web.request("POST", "/api/v1/jobs", payload)
    assert status == 409
    data = json.loads(body)
    assert data["requires_confirmation"] is True
    assert [item["session_id"] for item in data["existing"]] == ["2"]
    assert web.captured == []

    status, headers, body = web.request("POST", "/api/v1/jobs", {**payload, "overwrite": True})
    assert status == 200
    created = json.loads(body)
    assert len(created["jobs"]) == 2
    assert [Path(job["out_dir"]).name for job in created["jobs"]] == [
        "2026-11-09 15-15",
        "2026-11-02 13-30",
    ]
    assert {job["course"] for job in created["jobs"]} == {"编译原理"}
    assert [job["week"] for job in created["jobs"]] == [2, 1]
    assert [options.session_id for options in web.captured] == ["3", "2"]
    assert [options.week for options in web.captured] == [2, 1]
    assert {options.course for options in web.captured} == {"编译原理"}


def test_repeated_create_for_same_session_replaces_completed_job(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    payload = {
        "source": "demo://course",
        "session_ids": ["2"],
        "agent": "none",
        "out_dir": str(web.output),
        "term_start": "2026-11-02",
    }
    status, headers, body = web.request("POST", "/api/v1/jobs", payload)
    assert status == 200
    first = json.loads(body)["jobs"][0]
    finished = web.manager.jobs[first["id"]]
    finished.status = "done"
    finished.out_dir = Path(first["out_dir"])
    status, headers, body = web.request("POST", "/api/v1/jobs", payload)
    assert status == 200
    assert first["id"] not in web.manager.jobs
    assert web.manager.jobs[json.loads(body)["jobs"][0]["id"]].title == "编译原理 2026-11-02 13:30"


def test_running_session_rejects_duplicate_submission(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    payload = {"source": "demo://course", "session_ids": ["2"], "agent": "none", "out_dir": str(web.output)}
    status, headers, body = web.request("POST", "/api/v1/jobs", payload)
    assert status == 200
    created = json.loads(body)["jobs"][0]
    web.manager.jobs[created["id"]].status = "running"
    status, headers, body = web.request("POST", "/api/v1/jobs", payload)
    assert status == 400
    assert "正在整理中" in json.loads(body)["error"]


def test_resolved_course_survives_restart_before_any_download(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    status, _, body = web.request(
        "POST",
        "/api/v1/resolve",
        {
            "source": "demo://course",
            "out_dir": str(web.output),
            "term_start": "2026-09-07",
        },
    )
    assert status == 200
    course = json.loads(body)
    assert web.captured == []
    status, _, body = web.request("GET", "/api/v1/courses")
    assert json.loads(body)["courses"][0]["id"] == course["course_id"]

    restored = server.JobManager(web.manager.store.path)
    try:
        saved = restored.courses[course["course_id"]]
        assert saved["source"] == "demo://course"
        assert saved["out_dir"] == str(web.output)
        assert saved["term_start"] == "2026-09-07"
        assert [item["week"] for item in saved["sessions"]] == [10, 9]
        assert restored.jobs == {}
    finally:
        restored.close()


def test_saved_course_refresh_and_continue_with_another_session(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    _, _, body = web.request(
        "POST",
        "/api/v1/jobs",
        {
            "source": "demo://course",
            "session_ids": ["2"],
            "agent": "none",
            "out_dir": str(web.output),
        },
    )
    first = json.loads(body)["job"]
    web.manager.jobs[first["id"]].status = "done"
    _write_result(Path(first["out_dir"]), complete=True)

    from course2md.resolvers import SessionInfo

    sessions = [SessionInfo("4", "2026-11-16", "编译原理 2026-11-16 13:30", course="编译原理"), *_demo_sessions()]
    patch_get_resolver(monkeypatch, lambda source: SimpleNamespace(name="demo", list_sessions=lambda source: sessions))
    status, _, body = web.request("POST", f"/api/v1/courses/{first['course_id']}/refresh", {})
    assert status == 200
    course = json.loads(body)
    assert len(web.manager.courses) == 1
    assert [item["id"] for item in course["sessions"]] == ["4", "3", "2"]
    assert course["sessions"][-1]["existing"]["complete"]
    assert course["sessions"][-1]["job_id"] == first["id"]

    status, _, body = web.request(
        "POST",
        "/api/v1/jobs",
        {
            "course_id": course["id"],
            "source": course["source"],
            "out_dir": course["out_dir"],
            "term_start": course["term_start"],
            "session_ids": ["4"],
            "agent": "none",
        },
    )
    assert status == 200
    second = json.loads(body)["job"]
    assert second["course_id"] == first["course_id"]
    assert second["session_id"] == "4"
    assert first["id"] in web.manager.jobs
    assert len(web.captured) == 2


def test_refresh_failure_keeps_saved_sessions(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    _, _, body = web.request("POST", "/api/v1/resolve", {"source": "demo://course"})
    course_id = json.loads(body)["course_id"]
    patch_get_resolver(monkeypatch, Mock(side_effect=ResolverError("凭证已过期")))
    status, _, _ = web.request("POST", f"/api/v1/courses/{course_id}/refresh", {})
    assert status == 400
    status, _, body = web.request("GET", f"/api/v1/courses/{course_id}")
    assert status == 200
    assert len(json.loads(body)["course"]["sessions"]) == 2


def test_all_job_states_and_custom_output_survive_restart(tmp_path, monkeypatch):
    from course2md.config import LlmConfig
    from course2md.pipeline import JobResult
    from course2md.stages.options import JobOptions

    root = tmp_path / "workspace"
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(root))
    manager = _idle_manager(root)
    for state in ("queued", "running", "done", "failed", "cancelled"):
        folder = tmp_path / "custom-output" / state
        folder.mkdir(parents=True)
        (folder / "课.srt").write_text("transcript", encoding="utf-8")
        job = server.Job(
            id=state,
            source="lecture.mp4",
            title="本地课程",
            out_dir=folder,
            status=state,
            course="本地课程",
            session_id="one",
            error="error" if state == "failed" else "",
        )
        job.options = JobOptions(
            source=job.source, out_dir=folder, ai=False, delete_video=False, llm=LlmConfig(api_key="secret-for-test")
        )
        if state == "done":
            job.result = JobResult(output_dir=folder)
        job.log("已保存的进度")
        job.emit({"kind": "assistant", "text": "已保存的输出"})
        manager.save_job(job)

    assert "secret-for-test" not in json.dumps(manager.store.load_jobs())
    monkeypatch.setattr(server.JobManager, "_dispatch", lambda self: None)
    restored = server.JobManager(builtin_worker=False)
    try:
        assert len(restored.jobs) == 5
        for state, job in restored.jobs.items():
            assert job.status == ("queued" if state in server.ACTIVE_STATUSES else state)
            assert job.session_id == "one"
            assert job.options.ai is False and job.options.delete_video is False
            assert job.files[0]["name"] == "课.srt"
            assert job.snapshot()["log_offset"] == job.snapshot()["event_offset"] == 1
            job.emit({"kind": "assistant", "text": "继续"})
            assert job.snapshot()["event_offset"] == 2
        assert restored.jobs["failed"].error == "error"
    finally:
        restored.close()


def test_replacing_job_also_replaces_persisted_record(web, monkeypatch):
    patch_get_resolver(monkeypatch, lambda source: _fake_resolver())
    payload = {"source": "demo://course", "session_ids": ["2"], "agent": "none"}
    _, _, body = web.request("POST", "/api/v1/jobs", payload)
    first_id = json.loads(body)["id"]
    previous = web.manager.jobs[first_id]
    previous.status = "done"
    status, _, body = web.request("POST", "/api/v1/jobs", payload)
    assert status == 200
    web.manager.save_job(previous)
    assert [item["id"] for item in web.manager.store.load_jobs()] == [json.loads(body)["id"]]


@pytest.fixture
def classroom_plugin(monkeypatch):
    from course2md import resolvers

    monkeypatch.setenv("COURSE2MD_RESOLVERS", "fake_resolvers:ClassroomResolver")
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: [])
    resolvers.reload()
    yield
    monkeypatch.delenv("COURSE2MD_RESOLVERS")
    resolvers.reload()


def test_plugin_credentials_change_reuses_course(web, classroom_plugin):
    _, _, body = web.request("POST", "/api/v1/resolve", {"source": "fake://course/1234?token=OLD"})
    course_id = json.loads(body)["course_id"]
    _, _, body = web.request("POST", "/api/v1/resolve", {"source": "fake://course/1234?token=NEW"})
    assert json.loads(body)["course_id"] == course_id
    assert len(web.manager.courses) == 1
    assert web.manager.courses[course_id]["source"].endswith("token=NEW")


def test_plugin_metadata_reaches_web_ui(web, classroom_plugin):
    _, _, body = web.request("GET", "/api/v1/config")
    assert json.loads(body)["resolvers"] == [
        {"name": "fakeclass", "label": "示例课堂", "hint": "粘贴示例课堂链接"},
        {"name": "local", "label": "本地视频", "hint": "本地视频文件或目录"},
    ]
    _, _, body = web.request("POST", "/api/v1/resolve", {"source": "fake://course/7?token=SECRET"})
    data = json.loads(body)
    assert (data["resolver"], data["resolver_label"]) == ("fakeclass", "示例课堂")
    assert data["view_options"] == [
        {"id": 1, "label": "讲台", "default": True},
        {"id": 2, "label": "黑板", "default": False},
    ]
    _, _, body = web.request(
        "POST",
        "/api/v1/jobs",
        {"source": "fake://course/7?token=SECRET", "session_ids": ["s2"], "agent": "none", "out_dir": str(web.output)},
    )
    job = json.loads(body)["job"]
    assert "SECRET" not in "\n".join(job["logs"])
    assert "fakeclass:course=7" in "\n".join(job["logs"])


def test_cross_site_writes_are_rejected(web):
    payload = {"source": "lecture.mp4"}
    status, _, _ = web.request("POST", "/api/v1/jobs", payload, headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _, _ = web.request("POST", "/api/v1/jobs", payload, headers={"Content-Type": "text/plain"})
    assert status == 403
    status, _, _ = web.request("POST", "/api/v1/jobs", payload, headers={"Origin": f"http://127.0.0.1:{web.port}"})
    assert status == 200
    assert len(web.captured) == 1


def test_rebound_host_names_are_rejected(web, monkeypatch):
    status, _, _ = web.request("GET", "/api/v1/config", headers={"Host": f"attacker.example:{web.port}"})
    assert status == 403
    status, _, _ = web.request("GET", "/api/v1/config", headers={"Host": f"localhost:{web.port}"})
    assert status == 200
    status, _, _ = web.request("GET", "/api/v1/config", headers={"Host": f"127.0.0.1:{web.port + 1}"})
    assert status == 403
    monkeypatch.setenv("COURSE2MD_ALLOWED_HOSTS", "laptop.lan")
    status, _, _ = web.request("GET", "/api/v1/config", headers={"Host": f"laptop.lan:{web.port}"})
    assert status == 200


@pytest.mark.parametrize(
    "override",
    [
        {"formats": ["srt", "docx"]},
        {"formats": "srt"},
        {"frame_interval": 0},
        {"frame_interval": "fast"},
        {"llm_base": "file:///etc/passwd"},
        {"device": "tpu"},
        {"views": ["one"]},
    ],
)
def test_invalid_job_options_fail_before_enqueuing(web, override):
    status, _, body = web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4", **override})
    assert status == 400, body
    assert web.captured == []


def test_malformed_requests_return_client_errors(web):
    assert web.request("GET", "/api/v1/jobs/probe?since=abc")[0] == 400
    assert web.request("POST", "/api/v1/jobs", raw="{not json")[0] == 400
    assert web.request("POST", "/api/v1/jobs", raw="[1, 2]")[0] == 400


def test_failed_followup_keeps_completed_course_usable(tmp_path, monkeypatch):
    from course2md.config import LlmConfig
    from course2md.pipeline import JobResult
    from course2md.refine import conversation
    from course2md.stages.options import JobOptions

    root = tmp_path / "output"
    work = root / "notes" / "agent-workspace" / "one"
    work.mkdir(parents=True)
    detailed, brief = root / "课.notes.md", root / "课.brief.md"
    detailed.write_text("# 详细版\n", encoding="utf-8")
    brief.write_text("# 缩略版\n", encoding="utf-8")
    options = JobOptions(source="lecture.mp4", out_dir=root, llm=LlmConfig())
    result = JobResult(output_dir=root, notes=detailed, detailed_notes=detailed, brief_notes=brief, note_workspace=work)
    job = server.Job(
        id="course-one",
        source=options.source,
        title="课程",
        out_dir=root,
        status="done",
        agent="builtin",
        options=options,
        result=result,
        revision=1,
    )
    manager = _idle_manager(root)
    manager.send(job, "补充例题", "message-one")

    def fail(*args, **kwargs):
        raise conversation.LlmError("本轮回复格式无效")

    monkeypatch.setattr(conversation, "continue_notes", fail)
    manager._chat(*manager._queue.get_nowait())
    assert job.status == "done" and job.error == ""
    assert job.messages[-2]["status"] == "failed"
    assert job.messages[-1]["text"] == "本轮回复格式无效"
    assert job.snapshot()["can_chat"]


def test_large_files_are_streamed_with_exact_length(web):
    media = web.output / "media" / "lecture.mp4"
    media.parent.mkdir()
    media.write_bytes(b"0123456789" * 300_000)
    status, headers, body = web.request("GET", "/api/v1/jobs/probe/file?rel=" + quote("media/lecture.mp4"))
    assert status == 200 and headers["Content-Length"] == str(len(body)) == "3000000"
    assert body == media.read_bytes()


def test_web_passes_agent_provider_and_model(web, monkeypatch):
    from course2md import agents as agent

    monkeypatch.setattr(
        agent,
        "agent_model_options",
        lambda refresh=False: {
            "dsh": {
                "default_provider": "deepseek-official",
                "default_model": "deepseek-flash",
                "providers": {"newapi": ["grok-4.7"]},
            }
        },
    )
    monkeypatch.setenv("COURSE2MD_DSH_MODEL", "deepseek-v4-pro")
    status, _, body = web.request("GET", "/api/v1/config")
    config = json.loads(body)
    assert config["agent_models"]["dsh"]["providers"] == {"newapi": ["grok-4.7"]}
    assert config["agent_choices"]["dsh"] == {"provider": "", "model": "deepseek-v4-pro"}
    status, _, body = web.request(
        "POST",
        "/api/v1/jobs",
        {"source": "lecture.mp4", "agent": "dsh", "agent_provider": "newapi", "agent_model": "grok-4.7"},
    )
    assert status == 200
    options = web.captured[0]
    assert (options.agent.provider, options.agent.model) == ("newapi", "grok-4.7")
    job = json.loads(body)["job"]
    assert job["agent_model"] == "newapi / grok-4.7"
    status, _, body = web.request(
        "POST", "/api/v1/jobs", {"source": "lecture.mp4", "agent": "dsh", "agent_model": "bad name", "overwrite": True}
    )
    assert status == 400 and "模型名称无效" in json.loads(body)["error"]


def test_web_chooses_frame_selector_per_job(web, monkeypatch):
    monkeypatch.setenv("COURSE2MD_FRAME_SELECTOR", "classroom")
    _status, _headers, body = web.request("GET", "/api/v1/config")
    config = json.loads(body)
    assert config["frame_selector"] == "classroom"
    assert [item["name"] for item in config["frame_selectors"]][:2] == ["classroom", "general"]
    assert all(item["label"] and item["builtin"] for item in config["frame_selectors"][:2])

    status, _headers, _body = web.request(
        "POST", "/api/v1/jobs", {"source": "lecture.mp4", "frame_selector": "general"}
    )
    assert status == 200
    assert web.captured[-1].frame_selector == "general"

    status, _headers, body = web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4", "frame_selector": "missing"})
    assert status == 400
    assert "未安装画面筛选策略" in json.loads(body)["error"]
    status, _headers, _body = web.request("POST", "/api/v1/jobs", {"source": "lecture.mp4", "frame_selector": "../x"})
    assert status == 400

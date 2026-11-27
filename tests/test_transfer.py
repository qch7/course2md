"""Hub 间课程产物互传与便携产物包测试。"""

from __future__ import annotations

import json
import socket
import stat
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from course2md.agents import AgentConfig
from course2md.cli import main as cli_main
from course2md.config import AsrConfig, LlmConfig
from course2md.core.accounts import Accounts
from course2md.core.crypto import FieldCipher
from course2md.export import anki_text
from course2md.hub import service
from course2md.hub.app import create_app
from course2md.hub.transfer import TransferError, TransferPeerStore, validate_remote_hub_url
from course2md.jobs.bundle import BUNDLE_FORMAT, BUNDLE_VERSION, export_bundle, import_bundle
from course2md.jobs.manager import Job, JobManager, _collect_files
from course2md.pipeline import JobResult
from course2md.stages.options import JobOptions


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _LiveHub:
    """测试用的真实本地 HTTP Hub（uvicorn 后台线程）。"""

    def __init__(self, app, port: int) -> None:
        self.port = port
        self.url = f"http://127.0.0.1:{port}"
        self._server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> _LiveHub:
        self._thread.start()
        deadline = time.time() + 10.0
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("测试 Hub 启动超时")
            time.sleep(0.02)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


def _seed_completed_job(
    manager: JobManager,
    root: Path,
    *,
    owner: str = "local",
    course_title: str = "线性代数",
    session_id: str = "s1",
    session_title: str = "第 1 讲 矩阵运算",
    folder_name: str = "2026-09-10 08-00",
    source: str = "https://example.edu/play?id=1&token=SECRET_COOKIE_123",
    note_text: str = "# 矩阵运算\n\n![课件](../frames/f001.jpg)\n\n重点公式：$AB \\neq BA$。\n",
    brief_text: str = "# 矩阵运算（缩略版）\n\n- 矩阵乘法不满足交换律\n",
    custom_agent_cmd: list[str] | None = None,
    llm_base: str = "https://api.example.com/v1",
) -> tuple[dict[str, Any], Job]:
    out_dir = (root / course_title / folder_name).resolve()
    notes_dir = out_dir / "notes"
    frames_dir = out_dir / "frames"
    ws_dir = notes_dir / "agent-workspace" / "ws1"
    turns_dir = ws_dir / "turns" / "t1"
    media_dir = out_dir / "media"
    audio_dir = out_dir / "audio"
    for folder in (notes_dir, frames_dir, ws_dir, turns_dir, media_dir, audio_dir):
        folder.mkdir(parents=True, exist_ok=True)

    frame_path = frames_dir / "f001.jpg"
    frame_path.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-data")

    detailed = notes_dir / "lecture.notes.md"
    brief = notes_dir / "lecture.brief.md"
    detailed.write_text(note_text, encoding="utf-8")
    brief.write_text(brief_text, encoding="utf-8")

    (out_dir / "lecture.srt").write_text("1\n00:00:00,000 --> 00:00:05,000\n矩阵乘法\n", encoding="utf-8")
    (out_dir / "lecture.meta.json").write_text(json.dumps({"课程": course_title}, ensure_ascii=False), encoding="utf-8")

    # 模拟不应打包进产物包的临时/媒体/本机会话文件
    (media_dir / "video.mp4").write_bytes(b"fake-mp4-video")
    (audio_dir / "audio.wav").write_bytes(b"fake-wav-audio")
    (ws_dir / "agent-session.json").write_text(
        json.dumps({"backend": "codex", "session_id": "local-only"}), encoding="utf-8"
    )
    (turns_dir / "prompt.txt").write_text("scratch turn prompt", encoding="utf-8")

    context = {
        "title": session_title,
        "meta": {"课程": course_title},
        "segments": [{"start": 0.0, "end": 5.0, "text": "矩阵乘法一般不满足交换律"}],
        "figures": {"F1": {"ts": 2.0, "path": str(frame_path)}},
    }
    (ws_dir / "context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    lesson_record = {
        "overview": "介绍矩阵乘法的基本性质",
        "topics": [
            {
                "title": "矩阵乘法",
                "timestamp": "00:00:00",
                "explanation": "矩阵乘法一般不满足交换律。",
                "key_points": ["$AB \\neq BA$"],
                "figure_ids": ["F1"],
            }
        ],
        "examples": [],
        "teacher_points": [],
        "review_questions": ["为什么矩阵乘法不满足交换律？"],
        "uncertainties": [],
    }
    (ws_dir / "lesson.current.json").write_text(
        json.dumps(lesson_record, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    manifest = {
        "source": source,
        "course": course_title,
        "notes": str(detailed),
        "detailed_notes": str(detailed),
        "brief_notes": str(brief),
        "note_workspace": str(ws_dir),
    }
    (out_dir / "job.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    course = manager.remember_course(
        source,
        root,
        {
            "course": course_title,
            "resolver": "local",
            "resolver_label": "本地文件",
            "term_start": "2026-09-07",
            "term_source": "config",
            "sessions": [
                {
                    "id": session_id,
                    "date": "2026-09-10",
                    "title": session_title,
                    "views": 1,
                    "week": 1,
                    "course": course_title,
                    "meta": {},
                    "folder": str(out_dir),
                }
            ],
        },
        owner=owner,
    )

    options = JobOptions(
        source=source,
        out_dir=out_dir,
        course=course_title,
        session_id=session_id,
        session_date="2026-09-10",
        week=1,
        asr=AsrConfig(model="tiny", device="cpu"),
        llm=LlmConfig(base_url=llm_base, api_key="sk-secret-key-never-export", model="gpt-4o-mini"),
        agent=AgentConfig(preset="codex" if not custom_agent_cmd else "", command=list(custom_agent_cmd or [])),
    )
    result = JobResult(
        output_dir=out_dir,
        notes=detailed,
        detailed_notes=detailed,
        brief_notes=brief,
        note_workspace=ws_dir,
        timings={"整理": 12.5},
    )
    job = Job(
        id=f"job-{session_id}",
        source=source,
        title=session_title,
        out_dir=out_dir,
        course=course_title,
        course_id=course["id"],
        session_id=session_id,
        session_date="2026-09-10",
        week=1,
        status="done",
        stage="完成",
        progress=1.0,
        message="详细版与缩略版已生成",
        agent="codex",
        agent_model="gpt-5",
        owner=owner,
        timings={"整理": 12.5},
        messages=[
            {"id": "m1", "role": "user", "text": f"整理课程 {session_title}", "status": "done"},
            {"id": "m2", "role": "assistant", "text": "详细版与缩略版已生成", "status": "done"},
        ],
        revision=2,
        options=options,
        result=result,
    )
    job.files = _collect_files(result)
    manager.jobs[job.id] = job
    manager.save_job(job, replace_existing=True)

    # 记录两个笔记版本
    manager.versions.import_version(job.id, b"# v1 detailed\n", b"# v1 brief\n", "整理", "gpt-5", created_at=1000.0)
    manager.versions.import_version(
        job.id, note_text.encode("utf-8"), brief_text.encode("utf-8"), "对话：补充公式", "gpt-5", created_at=2000.0
    )
    return course, job


def test_bundle_roundtrip_sanitization_and_features(tmp_path: Path) -> None:
    src_root = tmp_path / "hub-a"
    dst_root = tmp_path / "hub-b"
    src_mgr = JobManager(src_root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    dst_mgr = JobManager(dst_root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    try:
        course, job = _seed_completed_job(src_mgr, src_root)
        bundle_path = tmp_path / "linear-algebra.c2m.zip"
        summary = export_bundle(src_mgr, [job], bundle_path, course=course)
        assert summary["job_count"] == 1
        assert bundle_path.is_file()

        # 校验 zip 内剥离了敏感信息与无关目录
        with zipfile.ZipFile(bundle_path, "r") as zf:
            names = set(zf.namelist())
            assert "bundle.json" in names
            assert not any("/media/" in n for n in names)
            assert not any("/audio/" in n for n in names)
            assert not any("agent-session.json" in n for n in names)
            assert not any("/turns/" in n for n in names)
            manifest_text = zf.read("bundle.json").decode("utf-8")
            assert "SECRET_COOKIE_123" not in manifest_text
            assert "sk-secret-key-never-export" not in manifest_text
            job_json_text = zf.read("jobs/job-0/files/job.json").decode("utf-8")
            assert "job://notes/lecture.notes.md" in job_json_text
            assert "SECRET_COOKIE_123" not in job_json_text

        # 导入到目标 Hub
        imported = import_bundle(dst_mgr, bundle_path, owner="local", root=dst_root, conflict="skip")
        assert len(imported["imported"]) == 1
        assert imported["overwritten"] == []
        assert imported["skipped"] == []
        assert imported["course"] is not None

        new_job_id = imported["imported"][0]["id"]
        new_job = dst_mgr.get(new_job_id)
        assert new_job is not None
        assert new_job.out_dir.resolve().is_relative_to(dst_root.resolve())
        assert new_job.snapshot()["can_chat"] is True
        assert new_job.result is not None and new_job.result.note_workspace is not None

        # 校验 job.json 路径已改写为目标 Hub 路径
        rewritten = json.loads((new_job.out_dir / "job.json").read_text(encoding="utf-8"))
        assert Path(rewritten["detailed_notes"]).is_file()
        assert Path(rewritten["detailed_notes"]).resolve().is_relative_to(dst_root.resolve())

        # 校验预览（含内联图片）、Anki 导出、版本历史均正常
        preview = service.preview(new_job, "notes/lecture.notes.md")
        assert preview["html"] and f"/api/v1/jobs/{new_job.id}/file?" in preview["html"]
        anki = anki_text(new_job.result.note_workspace)
        assert "矩阵乘法" in anki
        versions = dst_mgr.versions.list(new_job.id)
        assert len(versions) == 2
        assert versions[0]["label"] == "对话：补充公式"
        assert versions[1]["label"] == "整理"
        assert dst_mgr.versions.text(new_job.id, 1, "detailed") == "# v1 detailed\n"
    finally:
        src_mgr.close()
        dst_mgr.close()


def test_bundle_conflict_modes_and_course_merge(tmp_path: Path) -> None:
    src_root = tmp_path / "src"
    dst_root = tmp_path / "dst"
    src_mgr = JobManager(src_root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    dst_mgr = JobManager(dst_root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    try:
        course, job1 = _seed_completed_job(src_mgr, src_root, session_id="s1", note_text="# 第 1 版\n")
        b1 = tmp_path / "b1.c2m.zip"
        export_bundle(src_mgr, [job1], b1, course=course)

        # 目标 Hub 已有同一课程且带自己的认证链接
        existing_course = dst_mgr.remember_course(
            "https://example.edu/play?id=1&token=DST_OWN_SECRET",
            dst_root,
            {"course": "线性代数", "sessions": []},
            owner="local",
        )

        # 首次导入应合并到已有课程，且不覆盖目标 Hub 自己的来源链接
        res1 = import_bundle(dst_mgr, b1, owner="local", root=dst_root, conflict="skip")
        assert len(res1["imported"]) == 1
        assert res1["course"]["id"] == existing_course["id"]
        assert dst_mgr.courses[existing_course["id"]]["source"] == "https://example.edu/play?id=1&token=DST_OWN_SECRET"
        first_job_id = res1["imported"][0]["id"]

        # skip 策略跳过冲突
        res_skip = import_bundle(dst_mgr, b1, owner="local", root=dst_root, conflict="skip")
        assert res_skip["imported"] == []
        assert len(res_skip["skipped"]) == 1

        # 修改源笔记后用 overwrite 覆盖，应保留同一 job_id 并更新内容
        assert job1.result and job1.result.detailed_notes
        job1.result.detailed_notes.write_text("# 更新后的笔记\n", encoding="utf-8")
        b2 = tmp_path / "b2.c2m.zip"
        export_bundle(src_mgr, [job1], b2, course=course)
        res_over = import_bundle(dst_mgr, b2, owner="local", root=dst_root, conflict="overwrite")
        assert len(res_over["overwritten"]) == 1
        assert res_over["overwritten"][0]["id"] == first_job_id
        dst_job = dst_mgr.get(first_job_id)
        assert dst_job and dst_job.result and dst_job.result.detailed_notes
        assert "更新后的笔记" in dst_job.result.detailed_notes.read_text(encoding="utf-8")

        # 目标课次处于运行中时，overwrite 不能破坏它
        dst_job.status = "running"
        res_active = import_bundle(dst_mgr, b2, owner="local", root=dst_root, conflict="overwrite")
        assert res_active["overwritten"] == []
        assert len(res_active["skipped"]) == 1
        assert "正在处理中" in res_active["skipped"][0]["reason"]
        dst_job.status = "done"

        # copy 策略另存副本到新目录
        res_copy = import_bundle(dst_mgr, b2, owner="local", root=dst_root, conflict="copy")
        assert len(res_copy["imported"]) == 1
        copy_job_id = res_copy["imported"][0]["id"]
        assert copy_job_id != first_job_id
        copy_job = dst_mgr.get(copy_job_id)
        assert copy_job is not None and copy_job.out_dir.name.endswith("-2")
    finally:
        src_mgr.close()
        dst_mgr.close()


def test_bundle_security_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dst_root = tmp_path / "dst"
    mgr = JobManager(dst_root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    try:
        # 1. 无效 zip
        bad = tmp_path / "not-a-zip.c2m.zip"
        bad.write_bytes(b"not a zip")
        with pytest.raises(ValueError, match="ZIP"):
            import_bundle(mgr, bad, owner="local", root=dst_root)

        # 2. 缺少 bundle.json
        no_manifest = tmp_path / "empty.c2m.zip"
        with zipfile.ZipFile(no_manifest, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with pytest.raises(ValueError, match=r"bundle\.json"):
            import_bundle(mgr, no_manifest, owner="local", root=dst_root)

        # 3. Zip-slip 越界路径
        slip = tmp_path / "slip.c2m.zip"
        with zipfile.ZipFile(slip, "w") as zf:
            zf.writestr("../escape.txt", "boom")
        with pytest.raises(ValueError, match="越界"):
            import_bundle(mgr, slip, owner="local", root=dst_root)

        # 4. 符号链接拒绝
        symlink_zip = tmp_path / "symlink.c2m.zip"
        with zipfile.ZipFile(symlink_zip, "w") as zf:
            info = zipfile.ZipInfo("jobs/job-0/files/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, "/etc/passwd")
        with pytest.raises(ValueError, match="符号链接"):
            import_bundle(mgr, symlink_zip, owner="local", root=dst_root)

        # 5. SHA-256 篡改校验
        tampered = tmp_path / "tampered.c2m.zip"
        manifest = {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "jobs": [
                {
                    "key": "job-0",
                    "title": "篡改测试",
                    "files": [{"rel": "notes/a.notes.md", "size": 5, "sha256": "0" * 64}],
                }
            ],
        }
        with zipfile.ZipFile(tampered, "w") as zf:
            zf.writestr("bundle.json", json.dumps(manifest))
            zf.writestr("jobs/job-0/files/notes/a.notes.md", "hello")
        with pytest.raises(ValueError, match="校验失败"):
            import_bundle(mgr, tampered, owner="local", root=dst_root)

        # 6. 服务器普通用户导入剥离自定义命令和任意 LLM 地址
        monkeypatch.setenv("COURSE2MD_LLM_BASE", "https://server-llm.example.com/v1")
        src_root = tmp_path / "src"
        src_mgr = JobManager(src_root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
        try:
            course, job = _seed_completed_job(
                src_mgr,
                src_root,
                custom_agent_cmd=["evil-cmd", "--run"],
                llm_base="https://attacker.example.com/v1",
            )
            bundle = tmp_path / "restricted.c2m.zip"
            export_bundle(src_mgr, [job], bundle, course=course)
            res = import_bundle(
                mgr, bundle, owner="u1", root=dst_root / "users" / "u1", server_mode=True, is_admin=False
            )
            imported_job = mgr.get(res["imported"][0]["id"])
            assert imported_job is not None and imported_job.options is not None
            assert imported_job.options.agent.command == []
            assert imported_job.options.llm.base_url == "https://server-llm.example.com/v1"
        finally:
            src_mgr.close()
    finally:
        mgr.close()


def test_peer_store_and_ssrf_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cipher = FieldCipher("unit-test-secret-key")
    state = tmp_path / "hub-peers.json"
    store = TransferPeerStore(state, cipher=cipher)
    peer_a = store.add("user-a", "https://hub-b.example.com/", token="c2m_api_secret_token_1234", name="实验室")
    peer_b = store.add("user-b", "https://other.example.com", token="c2m_other_9999", name="另一个")

    assert peer_a["has_token"] is True
    assert "secret" not in peer_a["token_hint"]
    assert len(store.list_for("user-a")) == 1
    assert len(store.list_for("user-b")) == 1
    assert store.get_for("user-a", peer_b["id"]) is None

    # 磁盘上已加密保存，不出现明文令牌
    disk_text = state.read_text(encoding="utf-8")
    assert "c2m_api_secret_token_1234" not in disk_text
    assert "enc:v1:" in disk_text

    # 重新加载后正确解密
    reloaded = TransferPeerStore(state, cipher=cipher)
    loaded_a = reloaded.get_for("user-a", peer_a["id"])
    assert loaded_a is not None and loaded_a.token == "c2m_api_secret_token_1234"

    # SSRF / 自环检查
    with pytest.raises(TransferError, match="自身"):
        validate_remote_hub_url("http://127.0.0.1:8765", local_origins={"http://127.0.0.1:8765"})
    assert validate_remote_hub_url("http://127.0.0.1:8766", server_mode=False) == "http://127.0.0.1:8766"
    with pytest.raises(TransferError, match="安全限制"):
        validate_remote_hub_url("http://169.254.169.254/latest", server_mode=True, is_admin=True)
    with pytest.raises(TransferError, match="普通用户不能"):
        validate_remote_hub_url("http://127.0.0.1:8766", server_mode=True, is_admin=False)
    monkeypatch.setenv("COURSE2MD_ALLOW_PRIVATE_TRANSFER", "1")
    assert validate_remote_hub_url("http://127.0.0.1:8766", server_mode=True, is_admin=False) == "http://127.0.0.1:8766"


@pytest.mark.integration
def test_hub_to_hub_live_push_pull_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """真实启动本地 Hub A 与服务器 Hub B，验证端到端推送、浏览、拉取、鉴权与 CLI 互传。"""
    root_a = tmp_path / "hub-a"
    root_b = tmp_path / "hub-b"
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(root_b))
    monkeypatch.setattr("course2md.cli.load_settings", lambda: (None, None))
    monkeypatch.setattr("course2md.cli.bootstrap_runtime", lambda: None)

    mgr_a = JobManager(root_a / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    mgr_b = JobManager(root_b / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    accounts_b = Accounts(mgr_b.store.db, registration="open")
    setup_tok = accounts_b.begin_setup()
    assert setup_tok
    user_b = accounts_b.setup("alice", "password1234", setup_tok)
    token_b = accounts_b.issue(user_b, "api", "互传令牌")

    port_a, port_b = _free_port(), _free_port()
    app_a = create_app(mgr_a, bind_host="127.0.0.1", web_dist=tmp_path / "no-web")
    app_b = create_app(mgr_b, bind_host="127.0.0.1", accounts=accounts_b, web_dist=tmp_path / "no-web")

    try:
        course_a, job_a = _seed_completed_job(
            mgr_a, root_a, course_title="操作系统", session_id="os1", session_title="第 1 讲 进程"
        )
        # 在 Hub B 上也放一门课程，供 Hub A 拉取
        user_b_root = root_b / "users" / user_b.id
        course_b, job_b = _seed_completed_job(
            mgr_b,
            user_b_root,
            owner=user_b.id,
            course_title="计算机网络",
            session_id="net1",
            session_title="第 1 讲 TCP",
            note_text="# TCP 协议\n\n三次握手详解。\n",
        )

        with _LiveHub(app_a, port_a) as hub_a, _LiveHub(app_b, port_b) as hub_b:
            client_a = TestClient(app_a, base_url="http://127.0.0.1")
            client_b = TestClient(app_b, base_url="http://127.0.0.1")

            # 1. 在 Hub A 保存 Hub B 为对端
            peer_resp = client_a.post(
                "/api/v1/transfer/peers",
                json={"name": "云端 Hub B", "hub_url": hub_b.url, "token": token_b},
            )
            assert peer_resp.status_code == 200, peer_resp.text
            peer_id = peer_resp.json()["peer"]["id"]

            # 2. 鉴权失败返回清晰中文错误
            bad_auth = client_a.post("/api/v1/transfer/remote", json={"hub_url": hub_b.url, "token": "wrong-token"})
            assert bad_auth.status_code == 401
            assert "令牌" in bad_auth.json()["error"] or "登录" in bad_auth.json()["error"]

            # 3. Hub A 推送课程到 Hub B
            push_resp = client_a.post(
                "/api/v1/transfer/push",
                json={"peer_id": peer_id, "course_id": course_a["id"], "conflict": "skip"},
            )
            assert push_resp.status_code == 200, push_resp.text
            push_data = push_resp.json()
            assert push_data["direction"] == "push"
            assert len(push_data["imported"]) == 1
            pushed_job_id = push_data["imported"][0]["id"]

            # 验证 Hub B 上可预览笔记、看版本、导出 Anki
            headers_b = {"Authorization": f"Bearer {token_b}"}
            preview_b = client_b.get(
                f"/api/v1/jobs/{pushed_job_id}/preview",
                params={"rel": "notes/lecture.notes.md"},
                headers=headers_b,
            )
            assert preview_b.status_code == 200
            assert "矩阵运算" in preview_b.json()["text"]
            versions_b = client_b.get(f"/api/v1/jobs/{pushed_job_id}/versions", headers=headers_b)
            assert versions_b.status_code == 200 and len(versions_b.json()["versions"]) == 2
            anki_b = client_b.get(f"/api/v1/jobs/{pushed_job_id}/export/anki", headers=headers_b)
            assert anki_b.status_code == 200 and "矩阵乘法" in anki_b.text

            # 4. Hub A 浏览 Hub B 的远程目录并拉取「计算机网络」
            remote_resp = client_a.post("/api/v1/transfer/remote", json={"peer_id": peer_id})
            assert remote_resp.status_code == 200, remote_resp.text
            remote_courses = remote_resp.json()["courses"]
            net_course = next(item for item in remote_courses if item["title"] == "计算机网络")
            assert len(net_course["jobs"]) == 1 and net_course["jobs"][0]["transferable"] is True

            pull_resp = client_a.post(
                "/api/v1/transfer/pull",
                json={"peer_id": peer_id, "course_id": net_course["id"], "out_dir": str(root_a), "conflict": "skip"},
            )
            assert pull_resp.status_code == 200, pull_resp.text
            pull_data = pull_resp.json()
            assert pull_data["direction"] == "pull"
            assert len(pull_data["imported"]) == 1
            pulled_id = pull_data["imported"][0]["id"]
            preview_a = client_a.get(f"/api/v1/jobs/{pulled_id}/preview", params={"rel": "notes/lecture.notes.md"})
            assert preview_a.status_code == 200 and "三次握手详解" in preview_a.json()["text"]

            # 5. 测试 CLI transfer list / export / import / copy
            assert cli_main(["transfer", "list", "--hub", hub_b.url, "--token", token_b]) == 0
            out_list = capsys.readouterr().out
            assert "计算机网络" in out_list and "操作系统" in out_list

            exported_zip = tmp_path / "cli-export.c2m.zip"
            assert (
                cli_main(
                    [
                        "transfer",
                        "export",
                        "--hub",
                        hub_b.url,
                        "--token",
                        token_b,
                        "--job",
                        job_b.id,
                        "-o",
                        str(exported_zip),
                    ]
                )
                == 0
            )
            assert exported_zip.is_file()

            assert cli_main(["transfer", "import", str(exported_zip), "--hub", hub_a.url, "--conflict", "copy"]) == 0
            out_imp = capsys.readouterr().out
            assert "新增 1 节" in out_imp

            assert (
                cli_main(
                    [
                        "transfer",
                        "copy",
                        "--from",
                        hub_a.url,
                        "--to",
                        hub_b.url,
                        "--to-token",
                        token_b,
                        "--job",
                        job_a.id,
                        "--conflict",
                        "skip",
                    ]
                )
                == 0
            )
            out_copy = capsys.readouterr().out
            assert "跳过 1 节" in out_copy
    finally:
        mgr_a.close()
        mgr_b.close()


def test_server_mode_transfer_user_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务器形态下，不同用户的对端 Hub 配置与产物导出严格隔离。"""
    root = tmp_path / "srv"
    monkeypatch.setenv("COURSE2MD_OUT_DIR", str(root))
    mgr = JobManager(root / ".course2md" / "workspace.sqlite3", builtin_worker=False)
    accounts = Accounts(mgr.store.db, registration="open")
    setup_tok = accounts.begin_setup()
    assert setup_tok
    admin = accounts.setup("admin1", "password1234", setup_tok)
    alice = accounts.register("alice2", "password1234", by=admin)
    tok_admin = accounts.issue(admin, "api", "t1")
    tok_alice = accounts.issue(alice, "api", "t2")
    app = create_app(mgr, bind_host="127.0.0.1", accounts=accounts, web_dist=tmp_path / "no-web")

    try:
        course, job = _seed_completed_job(mgr, root / "users" / admin.id, owner=admin.id, course_title="编译原理")
        client = TestClient(app, base_url="http://127.0.0.1")
        h_admin = {"Authorization": f"Bearer {tok_admin}"}
        h_alice = {"Authorization": f"Bearer {tok_alice}"}

        # admin 创建对端 Hub，alice 看不到也删不掉
        created = client.post(
            "/api/v1/transfer/peers",
            json={"name": "内网 Hub", "hub_url": "http://127.0.0.1:9999", "token": "tok"},
            headers=h_admin,
        )
        assert created.status_code == 200
        peer_id = created.json()["peer"]["id"]
        assert client.get("/api/v1/transfer/peers", headers=h_alice).json()["peers"] == []
        assert client.delete(f"/api/v1/transfer/peers/{peer_id}", headers=h_alice).status_code == 404

        # alice 无法导出 admin 的课次或课程产物包
        assert client.get(f"/api/v1/jobs/{job.id}/bundle", headers=h_alice).status_code == 404
        assert client.get(f"/api/v1/courses/{course['id']}/bundle", headers=h_alice).status_code == 404

        # admin 导出后再由 alice 以 application/zip 导入到 alice 自己的空间
        bundle_resp = client.get(f"/api/v1/courses/{course['id']}/bundle", headers=h_admin)
        assert bundle_resp.status_code == 200
        imp = client.post(
            "/api/v1/bundles/import?conflict=skip",
            content=bundle_resp.content,
            headers={**h_alice, "Content-Type": "application/zip"},
        )
        assert imp.status_code == 200, imp.text
        imported_id = imp.json()["imported"][0]["id"]
        imported_job = mgr.get(imported_id)
        assert imported_job is not None
        assert imported_job.owner == alice.id
        assert imported_job.out_dir.resolve().is_relative_to((root / "users" / alice.id).resolve())
    finally:
        mgr.close()

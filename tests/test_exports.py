"""导出账号权限、内容保留与增量同步。"""

import csv
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from course2md.cli import build_parser
from course2md.core.accounts import Accounts
from course2md.export import anki_text, sync_obsidian
from course2md.hub.app import create_app
from course2md.jobs.manager import Job, JobManager
from course2md.pipeline import JobResult


@pytest.fixture
def course_job(sample_course, tmp_path):
    course = sample_course
    detailed = course.root / "notes" / "course.notes.md"
    detailed.write_text("# 笔记\n![画面](../frames/page.jpg)\n", encoding="utf-8")
    brief = detailed.with_name("course.brief.md")
    brief.write_text("# 缩略版", encoding="utf-8")
    manager = JobManager(tmp_path / "workspace.sqlite3", builtin_worker=False)
    job = Job("one", "x", "编译原理", course.root, status="done", revision=1)
    job.result = JobResult(course.root, detailed_notes=detailed, brief_notes=brief, note_workspace=course.workspace)
    manager.jobs[job.id] = job
    manager._save(job)
    yield manager, job
    manager.close()


def test_saved_courses_with_retired_review_fields_still_load(course_job):
    manager, _job = course_job
    saved = manager.store.load_jobs()[0]
    saved.update(review_state={"old-topic": True}, review_revision=1)
    restored = manager._restore_job(saved)
    assert restored.status == "done" and restored.result.detailed_notes.is_file()
    manager._save(restored)
    assert "review_state" not in manager.store.load_jobs()[0]
    assert "review_revision" not in manager.store.load_jobs()[0]


def test_export_respects_account_ownership(course_job):
    manager, job = course_job
    accounts = Accounts(manager.store.db)
    app = create_app(manager, accounts=accounts)
    owner = accounts.setup("owner", "password-123", accounts.begin_setup() or "")
    job.owner = owner.id
    accounts.register("other", "password-123", by=owner)
    client = TestClient(app, base_url="https://course.example.com")
    assert client.get("/api/v1/jobs/one/export/anki").status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "other", "password": "password-123"}).status_code == 200
    assert client.get("/api/v1/jobs/one/export/anki").status_code == 404


def test_anki_export_preserves_formula_steps_and_uncertainty(sample_course, course_job):
    text = anki_text(sample_course.workspace)
    lines = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    rows = list(csv.reader(io.StringIO(lines), delimiter="\t"))
    assert len(rows) == 2
    assert "E→TE′" in rows[1][1] and "ε" in rows[1][1]
    assert "待核对" in rows[1][1]
    manager, _job = course_job
    response = TestClient(create_app(manager), base_url="http://127.0.0.1").get("/api/v1/jobs/one/export/anki")
    assert response.status_code == 200 and "attachment" in response.headers["Content-Disposition"]


def test_obsidian_sync_is_incremental_and_preserves_local_edits(sample_course, course_job, tmp_path):
    (sample_course.root / ".env").write_text("private-secret", encoding="utf-8")
    vault = tmp_path / "vault"
    first = sync_obsidian(sample_course.root, vault)
    target = Path(first["path"])
    assert len(first["updated"]) == 3
    assert (target / "frames" / "page.jpg").is_file()
    assert not (target / ".env").exists() and not (target / "notes" / "agent-workspace").exists()
    assert sync_obsidian(sample_course.root, vault)["updated"] == []
    original = sample_course.root / "notes" / "course.brief.md"
    original.write_text("# 更新后的缩略版", encoding="utf-8")
    assert sync_obsidian(sample_course.root, vault)["updated"] == ["notes/course.brief.md"]
    assert (target / "notes" / "course.brief.md").read_text(encoding="utf-8") == "# 更新后的缩略版"
    local = target / "notes" / "course.notes.md"
    local.write_text("我的复习批注", encoding="utf-8")
    result = sync_obsidian(sample_course.root, vault)
    assert result["conflicts"] == ["notes/course.notes.md"]
    assert local.read_text(encoding="utf-8") == "我的复习批注"
    with pytest.raises(ValueError, match="vault"):
        sync_obsidian(sample_course.root, vault, "../escape")


def test_obsidian_concurrent_sync_keeps_manifest_consistent(sample_course, course_job, tmp_path):
    vault = tmp_path / "vault"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: sync_obsidian(sample_course.root, vault), range(2)))
    assert sorted(len(result["updated"]) for result in results) == [0, 3]
    assert all(not result["conflicts"] for result in results)
    assert sync_obsidian(sample_course.root, vault)["updated"] == []


def test_export_and_sync_cli(sample_course, course_job, tmp_path, capsys):
    parser = build_parser()
    output = tmp_path / "export" / "cards.tsv"
    args = parser.parse_args(["export", str(sample_course.root), "--format", "anki", "-o", str(output)])
    assert args.func(args) == 0
    assert "E→TE′" in output.read_text(encoding="utf-8")
    vault = tmp_path / "vault"
    args = parser.parse_args(["sync", str(sample_course.root), "--vault", str(vault)])
    assert args.func(args) == 0
    local = next(vault.rglob("*.notes.md"))
    local.write_text("本地批注", encoding="utf-8")
    assert args.func(args) == 1
    assert "保留本地修改" in capsys.readouterr().out

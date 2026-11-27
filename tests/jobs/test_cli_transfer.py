"""`course2md transfer` 各子命令的分支：用假的 RemoteHubClient 替代真实 Hub。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from course2md.cli import main as cli_main
from course2md.hub.transfer import TransferError


class FakeRemote:
    """记录调用顺序与参数，下载时写入一个小文件，推送时按来源返回固定结果。"""

    calls: ClassVar[list[tuple[Any, ...]]] = []
    fail_on_push = False

    def __init__(self, url: str, token: str = "") -> None:
        self.url = url
        self.token = token

    def __enter__(self) -> FakeRemote:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def catalog(self) -> dict[str, Any]:
        return {"hub_url": self.url, "mode": "local", "courses": []}

    def download_course_bundle(
        self, course_id: str, target: Path, *, job_ids: list[str] | None = None, include_cache: bool = False
    ) -> None:
        FakeRemote.calls.append(("course", self.url, course_id, job_ids, include_cache))
        Path(target).write_bytes(b"zip")

    def download_job_bundle(self, job_id: str, target: Path, *, include_cache: bool = False) -> None:
        FakeRemote.calls.append(("job", self.url, job_id, include_cache))
        Path(target).write_bytes(b"zip")

    def push_bundle(self, path: Path, *, conflict: str = "skip") -> dict[str, Any]:
        if FakeRemote.fail_on_push:
            raise TransferError("对端拒绝")
        assert Path(path).is_file()
        FakeRemote.calls.append(("push", self.url, conflict, Path(path)))
        return {"imported": [{"id": f"n{len(FakeRemote.calls)}", "title": "新课次"}]}


@pytest.fixture(autouse=True)
def fake_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRemote.calls = []
    FakeRemote.fail_on_push = False
    monkeypatch.setattr("course2md.hub.transfer.RemoteHubClient", FakeRemote)


def test_list_without_courses(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["transfer", "list", "--hub", "http://hub-a"]) == 0
    out = capsys.readouterr().out
    assert "Hub：http://hub-a（local）" in out and "没有可传输的课程产物" in out


def test_export_requires_course_or_job(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="请指定 --course 或 --job"):
        cli_main(["transfer", "export", "-o", str(tmp_path / "x.zip")])


def test_export_course_passes_jobs_and_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "out.c2m.zip"
    argv = ["transfer", "export", "--course", "c1", "--job", "j1", "--with-cache", "-o", str(target)]
    assert cli_main(argv) == 0
    assert FakeRemote.calls == [("course", "http://127.0.0.1:8765", "c1", ["j1"], True)]
    assert "产物包已导出" in capsys.readouterr().out and target.is_file()


def test_import_missing_bundle_exits(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="找不到产物包"):
        cli_main(["transfer", "import", str(tmp_path / "missing.zip")])


def test_copy_requires_course_or_job() -> None:
    with pytest.raises(SystemExit, match="请指定 --course 或 --job"):
        cli_main(["transfer", "copy", "--to", "http://hub-b"])


def test_copy_course_downloads_once_and_removes_tempfile(capsys: pytest.CaptureFixture[str]) -> None:
    argv = [
        "transfer",
        "copy",
        "--from",
        "http://hub-a",
        "--to",
        "http://hub-b",
        "--course",
        "c1",
        "--conflict",
        "copy",
    ]
    assert cli_main(argv) == 0
    kinds = [call[0] for call in FakeRemote.calls]
    assert kinds == ["course", "push"]
    assert FakeRemote.calls[1][1:3] == ("http://hub-b", "copy")
    assert not FakeRemote.calls[1][3].exists()
    assert "新增 1 节" in capsys.readouterr().out


def test_copy_jobs_use_their_own_ids(capsys: pytest.CaptureFixture[str]) -> None:
    """多个 --job 逐个下载，闭包不能把所有请求都绑定到最后一个编号。"""
    argv = ["transfer", "copy", "--to", "http://hub-b", "--job", "j1", "--job", "j2"]
    assert cli_main(argv) == 0
    assert [call[2] for call in FakeRemote.calls if call[0] == "job"] == ["j1", "j2"]
    assert "新增 2 节" in capsys.readouterr().out


def test_copy_failure_reports_transfer_error_and_cleans_up(capsys: pytest.CaptureFixture[str]) -> None:
    FakeRemote.fail_on_push = True
    assert cli_main(["transfer", "copy", "--to", "http://hub-b", "--job", "j1"]) == 1
    assert "传输失败：对端拒绝" in capsys.readouterr().err

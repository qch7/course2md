"""小型 JSON 状态文件的读写约定。"""

import json
import stat
import sys

import pytest

from course2md.util.jsonfile import read_json_object, write_json_private


def test_round_trip_keeps_chinese_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "nested" / "state.json"
    write_json_private(path, {"version": 1, "name": "课程节点"}, prefix=".demo-")
    assert read_json_object(path) == {"version": 1, "name": "课程节点"}
    assert "课程节点" in path.read_text(encoding="utf-8")
    assert [item.name for item in path.parent.iterdir()] == ["state.json"]


def test_overwrite_replaces_previous_content(tmp_path):
    path = tmp_path / "state.json"
    write_json_private(path, {"a": 1})
    write_json_private(path, {"b": 2})
    assert read_json_object(path) == {"b": 2}


@pytest.mark.parametrize("content", ["", "{broken", "[1, 2]", '"text"', "null"])
def test_unreadable_or_non_object_files_read_as_none(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    assert read_json_object(path) is None


def test_missing_file_reads_as_none(tmp_path):
    assert read_json_object(tmp_path / "absent.json") is None


def test_failed_write_keeps_old_file_and_cleans_temp(tmp_path):
    path = tmp_path / "state.json"
    write_json_private(path, {"ok": True})
    with pytest.raises(TypeError):
        write_json_private(path, {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert [item.name for item in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不使用 POSIX 权限位")
def test_file_is_owner_only(tmp_path):
    path = tmp_path / "state.json"
    write_json_private(path, {"token": "x"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

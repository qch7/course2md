"""缓存 JSON 可读但关联文件丢失时重新派发对应阶段。"""

from types import SimpleNamespace

import pytest

from course2md.asr.frames import Frame
from course2md.core import Database, TaskQueue
from course2md.pipeline import JobOptions
from course2md.stages.artifacts import FrameSet, Notes, TranscriptFiles
from course2md.stages.base import StageContext
from course2md.stages.codec import cache_key, encode, options_to_dict
from course2md.stages.executor import QueueExecutor, _cached_files_exist
from course2md.storage import LocalFileStore


@pytest.mark.parametrize("same_run", [False, True])
def test_missing_screenshot_is_rebuilt_on_reuse_and_restart(tmp_path, monkeypatch, same_run):
    db = Database.at(tmp_path / "db.sqlite3")
    queue = TaskQueue(db, LocalFileStore(tmp_path / "objects"))
    options = JobOptions("x", tmp_path)
    frame = tmp_path / "page.jpg"
    frame.write_bytes(b"image")
    output = [FrameSet("one", [Frame(1, frame)])]
    payload = {"options": options_to_dict(options), "inputs": None, "run_id": "run"}
    key = cache_key("frames", payload["options"], None)
    old = queue.enqueue("job", "frames", payload, cache_key=key)
    queue.claim("worker", set())
    queue.complete(old, "worker", encode(output))
    frame.unlink()
    executor = QueueExecutor(queue, "job", reuse={"frames"}, run_id="run" if same_run else "new")

    def execute(task_id, *_args):
        assert task_id != old
        claimed = queue.claim("worker", {"frames"})
        assert claimed.id == task_id
        frame.write_bytes(b"rebuilt")
        queue.complete(task_id, "worker", encode(output))
        return queue.get(task_id)

    monkeypatch.setattr(executor, "_wait", execute)
    try:
        result = executor.run(SimpleNamespace(name="frames"), StageContext(options), None)
        assert result[0].frames[0].path.read_bytes() == b"rebuilt"
        assert len(queue.tasks_for_job("job")) == 2
    finally:
        db.dispose()


def test_note_cache_requires_referenced_images_and_workspace(sample_course):
    root = sample_course.root
    detailed, brief = root / "detailed.md", root / "brief.md"
    detailed.write_text("![画面](frames/page.jpg)", encoding="utf-8")
    brief.write_text("缩略版", encoding="utf-8")
    output = Notes(detailed, brief, sample_course.workspace)
    assert _cached_files_exist(output)
    sample_course.image.unlink()
    assert not _cached_files_exist(output)
    detailed.write_text("无配图", encoding="utf-8")
    assert _cached_files_exist(output)
    (sample_course.workspace / "context.json").unlink()
    assert not _cached_files_exist(output)


def test_transcript_file_cache_requires_metadata(tmp_path):
    text, metadata = tmp_path / "text.md", tmp_path / "meta.json"
    text.write_text("转写", encoding="utf-8")
    metadata.write_text("{}", encoding="utf-8")
    output = [TranscriptFiles("one", {"md": text}, metadata)]
    assert _cached_files_exist(output)
    metadata.unlink()
    assert not _cached_files_exist(output)

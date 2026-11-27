"""完整流水线经任务队列交给内置 Worker 执行，结果与进程内执行一致。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from course2md import pipeline
from course2md.agents import AgentConfig
from course2md.asr.formats import Segment
from course2md.core import Database, TaskQueue
from course2md.refine.lesson import NotePair
from course2md.resolvers import ResolverError
from course2md.resolvers.base import MediaRef
from course2md.stages import Cancelled
from course2md.stages import media as media_stage
from course2md.stages.executor import QueueExecutor
from course2md.stages.options import JobOptions
from course2md.storage import LocalFileStore
from course2md.worker import WorkerRunner


@pytest.fixture
def hub(tmp_path, monkeypatch):
    from course2md.refine import lesson

    refs = [MediaRef(str(tmp_path / "room.mp4"), "room.mp4", view=1)]
    calls = {"transcribe": 0, "notes": 0}

    def transcribe(samples):
        calls["transcribe"] += 1
        return [Segment(0, 2, "教室讲话。"), Segment(100, 102, "结尾。")]

    def build_notes(segments, title, *args, progress=None, on_output=None, **kwargs):
        calls["notes"] += 1
        progress(0.5, "整理中")
        if on_output:
            on_output({"type": "text", "text": "Agent 输出"})
        return NotePair(f"# 详细版 {calls['notes']}\n", "# 缩略版\n")

    monkeypatch.setattr(media_stage, "get_resolver", lambda source: SimpleNamespace(resolve=lambda *args: refs))
    monkeypatch.setattr(media_stage, "WhisperBackend", lambda cfg: SimpleNamespace(cfg=cfg, transcribe=transcribe))
    monkeypatch.setattr(media_stage.audio_mod, "prepare", lambda path, wav: (wav, 120))
    monkeypatch.setattr(media_stage.audio_mod, "read_wav", lambda path: [])
    monkeypatch.setattr(lesson, "build_note_pair", build_notes)

    db = Database.at(tmp_path / "hub.sqlite3")
    queue = TaskQueue(db, LocalFileStore(tmp_path / "objects"))
    worker = WorkerRunner(
        queue,
        {"asr", "frames", "llm-api", "hub", "agent:custom"},
        worker_id="builtin",
        poll_interval=0.02,
        progress_interval=0,
    )
    worker.start()
    options = JobOptions(source="mock", out_dir=tmp_path / "out", proofread=False, frames=False)
    yield SimpleNamespace(queue=queue, worker=worker, options=options, calls=calls, tmp_path=tmp_path)
    worker.stop()
    db.dispose()


def _run(hub, job_id="job", reuse=frozenset(), **kwargs):
    executor = QueueExecutor(hub.queue, job_id, reuse=reuse, poll_interval=0.02)
    return pipeline.run_job(hub.options, executor=executor, **kwargs), executor


def test_queue_run_matches_local_outputs(hub):
    progress, events = [], []
    result, executor = _run(hub, progress=lambda *args: progress.append(args), on_output=events.append)
    assert result.primary_media == "room"
    assert result.transcripts["srt"].is_file()
    assert result.detailed_notes.read_text(encoding="utf-8") == "# 详细版 1\n"
    tasks = hub.queue.tasks_for_job("job")
    assert [task["stage"] for task in tasks] == ["media", "output", "organize"]
    assert all(task["status"] == "done" for task in tasks)
    assert {task["stage"]: task["worker_id"] for task in tasks} == {
        "media": "builtin",
        "output": "hub",
        "organize": "builtin",
    }
    assert tasks[0]["requires"] == ["asr"]  # 替身解析器没有登记名称
    assert ("整理", pytest.approx(0.84), "整理中", 0.5) in progress
    assert events == [{"type": "text", "text": "Agent 输出"}]
    assert executor.media_worker == "builtin"


def test_rerun_reuses_completed_stages(hub):
    _run(hub, job_id="job")
    assert hub.calls == {"transcribe": 1, "notes": 1}
    result, _ = _run(hub, job_id="job", reuse={"media", "frames", "proofread", "output"})
    assert hub.calls == {"transcribe": 1, "notes": 2}  # 只重新整理
    assert result.detailed_notes.read_text(encoding="utf-8") == "# 详细版 2\n"
    stages = [task["stage"] for task in hub.queue.tasks_for_job("job")]
    assert stages == ["media", "output", "organize", "organize"]


def test_changed_options_are_not_reused(hub):
    _run(hub)
    hub.options.asr.model = "small"
    _run(hub, reuse={"media"})
    assert hub.calls["transcribe"] == 2


def test_stage_errors_keep_their_type(hub, monkeypatch):
    def missing(source):
        raise ResolverError("没有能处理该来源的解析器")

    monkeypatch.setattr(media_stage, "get_resolver", missing)
    with pytest.raises(ResolverError, match="没有能处理"):
        _run(hub)
    assert hub.queue.tasks_for_job("job")[0]["error_type"] == "ResolverError"


def test_cancel_reaches_the_worker(hub, monkeypatch):
    from course2md.refine import lesson

    started = threading.Event()

    def slow(*args, cancel=None, **kwargs):
        started.set()
        while not cancel.is_set():
            cancel.wait(0.01)
        raise Cancelled("任务已取消")

    monkeypatch.setattr(lesson, "build_note_pair", slow)
    hub.worker.lease_seconds = 0.3  # 让续约（以及取消通知）更频繁
    cancel = threading.Event()
    threading.Thread(target=lambda: (started.wait(5), cancel.set()), daemon=True).start()
    with pytest.raises(Cancelled):
        _run(hub, cancel=cancel)
    assert hub.queue.tasks_for_job("job")[-1]["status"] == "cancelled"


def test_waiting_for_missing_capability_is_reported(hub):
    hub.worker.stop()
    hub.options.agent = AgentConfig(command=["fake-agent"])
    progress = []
    cancel = threading.Event()
    executor = QueueExecutor(hub.queue, "job", poll_interval=0.02, wait_hint_after=0.05)

    def watch():
        while not any(item[0] == "等待节点" for item in progress):
            threading.Event().wait(0.02)
        cancel.set()

    threading.Thread(target=watch, daemon=True).start()
    with pytest.raises(Cancelled):
        pipeline.run_job(hub.options, executor=executor, cancel=cancel, progress=lambda *args: progress.append(args))
    message = next(item[2] for item in progress if item[0] == "等待节点")
    assert "asr" in message

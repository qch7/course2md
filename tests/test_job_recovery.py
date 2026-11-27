"""课程并行编排、保持课内顺序与重启恢复。"""

import threading

from course2md.jobs.manager import Job, JobManager
from course2md.pipeline import JobResult
from course2md.stages.base import StageContext
from course2md.stages.codec import cache_key, options_to_dict
from course2md.stages.executor import QueueExecutor, Suspended
from course2md.stages.options import JobOptions
from course2md.stages.output import OutputStage
from course2md.stages.proofread import ProofreadStage


def test_waiting_course_does_not_block_another_course(tmp_path, monkeypatch):
    waiting, other_done = threading.Event(), threading.Event()
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)

    def run(options, **kwargs):
        if options.source == "waiting":
            waiting.set()
            assert other_done.wait(4)
        else:
            other_done.set()
        return JobResult(output_dir=options.out_dir)

    monkeypatch.setattr("course2md.jobs.manager.run_job", run)
    try:
        for name in ("waiting", "ready"):
            if name == "ready":
                assert waiting.wait(4)
            manager.submit(Job(name, name, name, tmp_path / name), JobOptions(name, tmp_path / name))
        assert other_done.wait(4)
    finally:
        manager.close()


def test_restart_reconnects_to_existing_stage_instead_of_enqueuing_duplicate(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite3"
    manager = JobManager(path, builtin_worker=False)
    job = Job("recover", "x", "x", tmp_path / "out")
    options = JobOptions("x", job.out_dir, ai=False)
    job.options = options
    payload = {"options": options_to_dict(options), "inputs": None, "run_id": job.run_id}
    key = cache_key("output", payload["options"], None)
    task = manager.queue.enqueue(job.id, "output", payload, cache_key=key)
    manager.jobs[job.id] = job
    manager.save_job(job)
    manager.close()
    finished = threading.Event()

    def run(options, executor, **kwargs):
        assert executor.run(OutputStage(), StageContext(options), None) == []
        finished.set()
        return JobResult(output_dir=options.out_dir)

    monkeypatch.setattr("course2md.jobs.manager.run_job", run)
    restored = JobManager(path, builtin_worker=False)
    try:
        claimed = restored.queue.claim("w", set())
        assert claimed.id == task
        restored.queue.complete(task, "w", [])
        assert finished.wait(4)
        assert len(restored.queue.tasks_for_job(job.id)) == 1
    finally:
        restored.close()


def test_shutdown_suspends_wait_without_cancelling_remote_task(tmp_path):
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    options = JobOptions("x", tmp_path)
    stop = threading.Event()
    executor = QueueExecutor(manager.queue, "job", stop_event=stop)
    stop.set()
    try:
        try:
            executor.run(ProofreadStage(), StageContext(options), None)
        except Suspended:
            pass
        else:
            raise AssertionError("expected suspension")
        assert manager.queue.tasks_for_job("job")[0]["status"] == "pending"
    finally:
        manager.close()


def test_followups_for_one_job_keep_submission_order(tmp_path, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    manager = JobManager(tmp_path / "db.sqlite3", builtin_worker=False)
    from course2md.worker import WorkerRunner

    worker = WorkerRunner(manager.queue, {"llm-api"}, poll_interval=0.02).start()
    calls = []

    def run(options, **kwargs):
        started.set()
        assert release.wait(4)
        calls.append("pipeline")
        return JobResult(output_dir=options.out_dir, note_workspace=tmp_path, detailed_notes=tmp_path / "notes.md")

    def reply(*args, **kwargs):
        from course2md.refine.conversation import Reply

        calls.append(args[1])
        if args[1] == "second":
            finished.set()
        return Reply("ok", False)

    monkeypatch.setattr("course2md.jobs.manager.run_job", run)
    monkeypatch.setattr("course2md.refine.conversation.continue_notes", reply)
    job = Job("one", "x", "x", tmp_path / "out")
    try:
        manager.submit(job, JobOptions("x", job.out_dir))
        assert started.wait(4)
        manager.send(job, "first", "first")
        manager.send(job, "second", "second")
        release.set()
        assert finished.wait(4)
        assert calls == ["pipeline", "first", "second"]
    finally:
        release.set()
        manager.close()
        worker.stop()

"""命令行入口。"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .config import (
    AsrConfig,
    ConfigError,
    LlmConfig,
    bootstrap_runtime,
    default_agent,
    default_detail,
    default_formats,
    default_frame_interval,
    default_frame_selector,
    default_frames,
    default_out_dir,
    default_term_start,
    default_worker_dir,
    load_settings,
)
from .util.text import hhmmss

STAGE_ORDER = ("解析", "下载", "音频", "转写", "输出", "整理", "完成")


def _progress(stage: str, fraction: float, message: str, sub: float | None = None) -> None:
    """单行刷新进度，日志落到 stderr，不干扰管道输出。"""
    bar_len = 24
    filled = int(bar_len * max(0.0, min(1.0, fraction)))
    bar = "█" * filled + "·" * (bar_len - filled)
    line = f"\r[{stage}] {bar} {fraction * 100:5.1f}%  {message[:70]}"
    sys.stderr.write(line.ljust(110))
    sys.stderr.flush()
    if stage == "完成":
        sys.stderr.write("\n")


def _agent_from_args(choice, args):
    """按命令行选择整理后端；提供商与模型未指定时读取环境变量，CLI 也未配置时沿用其自身默认。"""
    from .agents import AgentError

    try:
        return default_agent(choice, provider=args.agent_provider, model=args.agent_model)
    except AgentError as exc:
        raise SystemExit(str(exc)) from None


def cmd_run(args) -> int:
    from .agents import detect_agents
    from .pipeline import run_job
    from .stages.options import JobOptions

    agent = _agent_from_args("none" if args.no_agent else args.agent or None, args)
    if args.agent not in ("", "none", "builtin") and not args.no_agent and not agent.resolved_command():
        raise SystemExit(f"找不到 agent：{args.agent}，可用：{', '.join(detect_agents()) or '无'}")

    asr_env = AsrConfig.from_env()
    options = JobOptions(
        source=args.source,
        out_dir=Path(args.out).expanduser().resolve(),
        formats=tuple(f.strip() for f in args.formats.split(",") if f.strip()),
        session_id=args.session,
        views=[int(v) for v in args.views.split(",")] if args.views else None,
        ai=not args.no_ai,
        proofread=not args.no_proofread,
        detail=not args.brief,
        frames=not args.no_frames,
        frame_interval=args.frame_interval,
        frame_selector=args.frame_selector,
        asr=replace(
            asr_env,
            model=args.model,
            device=args.device,
            compute_type=args.compute_type
            or (
                asr_env.compute_type
                if args.device == asr_env.device
                else ("int8_float16" if args.device == "cuda" else "int8")
            ),
        ),
        llm=replace(LlmConfig.from_env(), model=args.llm_model, base_url=args.llm_base),
        agent=agent,
        keep_audio=args.keep_audio,
    )
    result = run_job(options, progress=_progress)
    print(f"\n输出目录：{result.output_dir}")
    for transcripts in result.transcripts_by_media.values():
        for fmt, path in transcripts.items():
            print(f"  {fmt:4s} {path.name}")
    print(f"  整理方式：{result.backend}")
    if result.corrected:
        print(f"  AI 校对改动 {result.corrected} 处")
    if result.detailed_notes:
        print(f"  详细版 {result.detailed_notes.name}")
    if result.brief_notes:
        print(f"  缩略版 {result.brief_notes.name}")
    print(f"共 {result.segments} 段 / {result.chars} 字")
    for warning in result.warnings:
        print(f"! {warning}")
    return 0


def cmd_export(args) -> int:
    from .export import anki_text, job_workspace

    target = Path(args.out).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(anki_text(job_workspace(Path(args.source).expanduser().resolve())), encoding="utf-8")
    print(f"Anki 导出：{target}")
    return 0


def cmd_sync(args) -> int:
    from .export import sync_obsidian

    result = sync_obsidian(Path(args.source).expanduser(), Path(args.vault).expanduser(), args.folder)
    print(f"同步目录：{result['path']}，已更新 {len(result['updated'])} 个文件")
    for relative in result["conflicts"]:
        print(f"保留本地修改，需手动合并：{relative}")
    return int(bool(result["conflicts"]))


def _print_transfer_summary(result: dict) -> None:
    imported = result.get("imported") or []
    overwritten = result.get("overwritten") or []
    skipped = result.get("skipped") or []
    print(f"完成：新增 {len(imported)} 节，覆盖 {len(overwritten)} 节，跳过 {len(skipped)} 节")
    for item in imported:
        print(f"  + {item.get('title') or item.get('id')}")
    for item in overwritten:
        print(f"  ~ {item.get('title') or item.get('id')}（已覆盖）")
    for item in skipped:
        print(f"  - {item.get('title') or ''}（{item.get('reason') or '已跳过'}）")


def cmd_transfer(args) -> int:  # noqa: PLR0912, PLR0915  历史遗留的长函数，拆分后移除
    import os
    import tempfile

    from .hub.transfer import RemoteHubClient, TransferError

    action = args.transfer_action
    try:
        if action == "list":
            with RemoteHubClient(args.hub, args.token or "") as remote:
                catalog = remote.catalog()
            print(f"Hub：{catalog['hub_url']}（{catalog['mode']}）")
            courses = catalog.get("courses") or []
            if not courses:
                print("没有可传输的课程产物")
                return 0
            for course in courses:
                cid = course.get("id") or "-"
                print(f"\n课程 [{cid}] {course.get('title')}")
                for job in course.get("jobs") or []:
                    mark = "可传" if job.get("transferable") else "处理中/无文件"
                    date = job.get("session_date") or ""
                    print(f"  - [{job['id']}] {job.get('title')}  {date}  ({mark}, {job.get('file_count', 0)} 个文件)")
            return 0

        if action == "export":
            if not args.course and not args.job:
                raise SystemExit("请指定 --course 或 --job")
            target = Path(args.out).expanduser().resolve()
            with RemoteHubClient(args.hub, args.token or "") as remote:
                if args.course:
                    remote.download_course_bundle(
                        args.course, target, job_ids=args.job or None, include_cache=args.with_cache
                    )
                else:
                    remote.download_job_bundle(args.job[0], target, include_cache=args.with_cache)
            print(f"产物包已导出：{target}（{target.stat().st_size / 1024:.0f} KB）")
            return 0

        if action == "import":
            bundle_file = Path(args.bundle).expanduser().resolve()
            if not bundle_file.is_file():
                raise SystemExit(f"找不到产物包：{bundle_file}")
            with RemoteHubClient(args.hub, args.token or "") as remote:
                result = remote.push_bundle(bundle_file, conflict=args.conflict)
            _print_transfer_summary(result)
            return 0

        if action == "copy":
            if not args.course and not args.job:
                raise SystemExit("请指定 --course 或 --job")
            imported: list[dict] = []
            overwritten: list[dict] = []
            skipped: list[dict] = []
            with (
                RemoteHubClient(args.source_hub, args.source_token or "") as src,
                RemoteHubClient(args.target_hub, args.target_token or "") as dst,
            ):
                if args.course:
                    fd, tmp_name = tempfile.mkstemp(prefix="c2m-cli-", suffix=".c2m.zip")
                    os.close(fd)
                    tmp_path = Path(tmp_name)
                    try:
                        src.download_course_bundle(
                            args.course, tmp_path, job_ids=args.job or None, include_cache=args.with_cache
                        )
                        res = dst.push_bundle(tmp_path, conflict=args.conflict)
                        imported.extend(res.get("imported") or [])
                        overwritten.extend(res.get("overwritten") or [])
                        skipped.extend(res.get("skipped") or [])
                    finally:
                        tmp_path.unlink(missing_ok=True)
                else:
                    for jid in args.job:
                        fd, tmp_name = tempfile.mkstemp(prefix="c2m-cli-", suffix=".c2m.zip")
                        os.close(fd)
                        tmp_path = Path(tmp_name)
                        try:
                            src.download_job_bundle(jid, tmp_path, include_cache=args.with_cache)
                            res = dst.push_bundle(tmp_path, conflict=args.conflict)
                            imported.extend(res.get("imported") or [])
                            overwritten.extend(res.get("overwritten") or [])
                            skipped.extend(res.get("skipped") or [])
                        finally:
                            tmp_path.unlink(missing_ok=True)
            _print_transfer_summary({"imported": imported, "overwritten": overwritten, "skipped": skipped})
            return 0
    except TransferError as exc:
        print(f"传输失败：{exc}", file=sys.stderr)
        return 1
    return 2


def cmd_list(args) -> int:
    from .resolvers import get_resolver, parse_date, resolve_term_start, view_options, week_of
    from .resolvers.week import TERM_START_SOURCES

    resolver = get_resolver(args.source)
    sessions = resolver.list_sessions(args.source)
    hint = next((s.meta.get("term_start") for s in sessions if s.meta.get("term_start")), "")
    term_start, basis = resolve_term_start(default_term_start(), hint, [session.date for session in sessions])
    print(f"解析器：{getattr(resolver, 'label', '') or resolver.name}（{resolver.name}），共 {len(sessions)} 个课次")
    options = view_options(resolver, args.source)
    if options:
        print("可选机位：" + "，".join(f"{o.id}={o.label}{'（默认）' if o.default else ''}" for o in options))
    if term_start:
        print(f"第 1 周：{term_start.isoformat()}（依据：{TERM_START_SOURCES.get(basis, basis)}）")
    for session in sessions:
        date = session.date or session.meta.get("begin", "")
        day = parse_date(session.date)
        week = week_of(day, term_start) if day and term_start else None
        label = f"第 {week:>2} 周" if week else "    -   "
        print(f"  {label} {date:20s} id={session.session_id:10s} {session.views} 机位  {session.title}")
    return 0


def cmd_refine(args) -> int:
    from .agents import AgentError, preset_names
    from .asr.formats import load_segments
    from .frames.sampling import extract
    from .refine import LlmError
    from .refine.lesson import build_note_pair, focus_timestamps, save_note_pair
    from .util.text import ensure_dir

    started = time.perf_counter()
    timings: dict[str, float] = {}
    path = Path(args.transcript)
    segments = load_segments(path)
    if not segments:
        raise SystemExit(f"{path} 里没有解析到带时间戳的内容")
    title = args.title or path.stem
    out = Path(args.out) if args.out else path.with_suffix(".notes.md")

    llm_cfg = replace(LlmConfig.from_env(), model=args.llm_model, base_url=args.llm_base)
    agent_cfg = _agent_from_args(args.agent, args)
    command = agent_cfg.resolved_command()
    if args.agent in preset_names() and not command:
        raise SystemExit(f"找不到 {args.agent}，请安装对应 CLI 或检查启动路径配置")
    root = ensure_dir(out.parent / "agent-workspace" / uuid.uuid4().hex)
    media = [Path(value) for value in args.media]
    if not media:
        media = [
            candidate
            for candidate in (path.parent / "media").glob(f"{path.stem}.*")
            if candidate.suffix.lower() in (".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts")
        ]
    frames = []
    if not args.no_frames:
        phase_started = time.perf_counter()
        for index, source in enumerate(media, 1):
            if not source.is_file():
                raise SystemExit(f"视频文件不存在：{source}")
            frames.extend(
                extract(
                    source,
                    root / "frames",
                    interval=args.frame_interval,
                    max_frames=None,
                    prefix=f"view-{index}_",
                    focus_times=focus_timestamps(segments),
                    selector_name=args.frame_selector,
                    progress=lambda f, m: _progress("画面", f * 0.15, m),
                )
            )
        timings["frames"] = time.perf_counter() - phase_started
    try:
        pair = build_note_pair(
            segments,
            title,
            {"时长": hhmmss(segments[-1].end)},
            llm_cfg,
            agent_cfg,
            root,
            frames=frames,
            frame_selector=args.frame_selector,
            progress=lambda f, m: _progress("整理", 0.15 + f * 0.8, m),
        )
    except (LlmError, AgentError) as exc:
        raise SystemExit(f"整理失败：{exc}") from exc
    timings.update(pair.timings)
    detailed_path, brief_path = save_note_pair(pair, out)
    timings["total"] = time.perf_counter() - started
    backend = (agent_cfg.preset or "custom") if command else "builtin"
    timing_path = out.with_suffix(".timings.json")
    timing_path.write_text(
        json.dumps(
            {
                "backend": backend,
                "transcript": str(path.resolve()),
                "audio_seconds": max(segment.end for segment in segments),
                "segments": len(segments),
                "input_chars": sum(len(segment.text) for segment in segments),
                "model_calls": pair.model_calls,
                "images_extracted": len(frames),
                "images_provided": pair.images_provided,
                "images_used": len(pair.figures),
                "reasoning_effort": agent_cfg.reasoning_effort if agent_cfg.preset else None,
                "agent_provider": agent_cfg.provider or None,
                "agent_model": agent_cfg.model or None,
                "workspace": str(root.resolve()),
                "output": str(detailed_path.resolve()),
                "brief_output": str(brief_path.resolve()),
                "output_chars": len(pair.detailed),
                "brief_chars": len(pair.brief),
                "timings_seconds": {key: round(value, 3) for key, value in timings.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\n详细版已写入 {detailed_path}（{backend}，{len(pair.detailed)} 字）")
    print(f"缩略版已写入 {brief_path}（{len(pair.brief)} 字）")
    print(f"整理耗时 {timings['total']:.1f} 秒，计时记录 {timing_path}")
    return 0


def cmd_doctor(_args) -> int:
    from .doctor import main as doctor_main

    return doctor_main()


def cmd_config(args) -> int:
    from .config.files import config_path, render_example_toml

    path = config_path()
    if args.init:
        if path.exists() and not args.force:
            print(f"配置文件已存在：{path}（加 --force 覆盖）", file=sys.stderr)
            return 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_example_toml(), encoding="utf-8")
        print(f"已写入配置模板：{path}")
        return 0
    print(f"{path}（{'已存在' if path.is_file() else '未创建，可用 course2md config --init 生成'}）")
    return 0


def cmd_worker(args) -> int:
    from .config import env
    from .worker import detect_capabilities
    from .worker.client import HttpTransport, HubError, RemoteWorker

    hub = args.hub or env("COURSE2MD_HUB_URL")
    token = args.token or env("COURSE2MD_WORKER_TOKEN")
    if not hub or not token:
        print("需要 --hub 与 --token（或环境变量 COURSE2MD_HUB_URL / COURSE2MD_WORKER_TOKEN）", file=sys.stderr)
        return 2
    capabilities = detect_capabilities(agents=not args.no_agents) | set(args.capability or [])
    capabilities -= set(args.without or [])
    work_dir = Path(args.work_dir).expanduser() if args.work_dir else default_worker_dir()
    worker = RemoteWorker(
        HttpTransport(hub, token), capabilities, work_dir.resolve(), name=args.name or env("COURSE2MD_WORKER_NAME")
    )
    try:
        worker.register()
    except (HubError, OSError) as exc:
        print(f"无法连接 Hub：{exc}", file=sys.stderr)
        return 1
    print(f"已连接 {hub}，节点 {worker.worker_id}，能力：{', '.join(sorted(capabilities))}")
    print(f"工作目录 {work_dir}（下载的视频留在这里）；Ctrl+C 退出")
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        worker.stop(timeout=1)
    return 0


def cmd_worker_token(args) -> int:
    from .core.queue import TaskQueue
    from .jobs.workspace_store import WorkspaceStore
    from .storage import LocalFileStore

    store = WorkspaceStore(
        Path(args.out_dir or default_out_dir()).expanduser().resolve() / ".course2md" / "workspace.sqlite3"
    )
    queue = TaskQueue(store.db, LocalFileStore(store.path.parent / "objects"))
    worker_id, token = queue.create_worker_token(args.name)
    print(f"节点 {worker_id}（{args.name}）的令牌，只显示这一次：")
    print(token)
    return 0


def cmd_server(args) -> int:
    from .hub.app import serve_server

    serve_server(host=args.host, port=args.port, with_node=args.with_node)
    return 0


def cmd_web(args) -> int:
    from .hub.app import serve

    serve(host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def build_parser() -> argparse.ArgumentParser:  # noqa: PLR0915  历史遗留的长函数，拆分后移除
    asr_env = AsrConfig.from_env()
    llm_env = LlmConfig.from_env()
    parser = argparse.ArgumentParser(prog="course2md", description="课程回放 → 结构化 Markdown 笔记")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="完整流水线：解析 → 转写 → 整理")
    run.add_argument("source", help="播放页链接或本地文件/目录")
    run.add_argument("-o", "--out", default=default_out_dir(), help="输出目录（默认 %(default)s）")
    run.add_argument("--session", help="指定课次编号（见 course2md list），格式由解析器决定")
    run.add_argument("--views", help="机位编号，逗号分隔，如 1,5（解析器支持多机位时有效）")
    run.add_argument("--formats", default=",".join(default_formats()), help="输出格式，逗号分隔（默认 %(default)s）")
    run.add_argument("--model", default=asr_env.model, help="Whisper 模型名或本地目录（默认 %(default)s）")
    run.add_argument("--device", default=asr_env.device, choices=["cuda", "cpu"])
    run.add_argument("--compute-type", default=None, help="int8_float16 / float16 / int8（默认按设备与环境配置选择）")
    run.add_argument("--llm-model", default=llm_env.model, help="整理用的模型（默认 %(default)s）")
    run.add_argument("--llm-base", default=llm_env.base_url, help="OpenAI 兼容接口地址（默认 %(default)s）")
    run.add_argument("--no-ai", action="store_true", help="完全跳过 AI（校对与笔记）")
    run.add_argument("--no-proofread", action="store_true", help="不做 AI 校对，但仍生成笔记")
    run.add_argument("--brief", action="store_true", default=not default_detail(), help="优先展示缩略版，两版均会生成")
    run.add_argument("--no-frames", action="store_true", default=not default_frames(), help="不抽画面帧，只依据转写稿")
    run.add_argument(
        "--frame-interval",
        type=float,
        default=default_frame_interval(),
        help="画面采样间隔秒数，越小越不容易漏翻页（默认 %(default)s）",
    )
    run.add_argument("--frame-selector", default=default_frame_selector(), help="画面筛选策略（默认 %(default)s）")
    run.add_argument("--agent", default="", help="用外部 Agent 整理（dsh / codex，也可填自定义命令）")
    run.add_argument("--no-agent", action="store_true", help="强制用内置单次调用整理")
    run.add_argument(
        "--agent-provider",
        default=None,
        help="DSH / Codex 使用的提供商（默认读取 COURSE2MD_DSH_PROVIDER 或 COURSE2MD_CODEX_PROVIDER）",
    )
    run.add_argument(
        "--agent-model",
        default=None,
        help="DSH / Codex 使用的模型（默认读取 COURSE2MD_DSH_MODEL 或 COURSE2MD_CODEX_MODEL）",
    )
    run.add_argument("--keep-audio", action="store_true", help="保留中间 WAV")
    run.set_defaults(func=cmd_run)

    list_cmd = sub.add_parser("list", help="列出可下载的课次")
    list_cmd.add_argument("source")
    list_cmd.set_defaults(func=cmd_list)

    refine = sub.add_parser("refine", help="把已有转写稿整理成笔记")
    refine.add_argument("transcript", help=".srt 或 .md 转写稿")
    refine.add_argument("-o", "--out", help="输出路径")
    refine.add_argument("--title", help="笔记标题")
    refine.add_argument("--llm-model", default=llm_env.model)
    refine.add_argument("--llm-base", default=llm_env.base_url)
    refine.add_argument("--agent", default=None, help="整理方式：dsh / codex / none（默认读取环境并自动探测）")
    refine.add_argument(
        "--agent-provider",
        default=None,
        help="DSH / Codex 使用的提供商（默认读取 COURSE2MD_DSH_PROVIDER 或 COURSE2MD_CODEX_PROVIDER）",
    )
    refine.add_argument(
        "--agent-model",
        default=None,
        help="DSH / Codex 使用的模型（默认读取 COURSE2MD_DSH_MODEL 或 COURSE2MD_CODEX_MODEL）",
    )
    refine.add_argument(
        "--media", action="append", default=[], help="参考视频路径，可重复指定；默认查找转写稿旁的 media 目录"
    )
    refine.add_argument("--no-frames", action="store_true", default=not default_frames(), help="仅使用转写稿")
    refine.add_argument("--frame-interval", type=float, default=default_frame_interval(), help="画面采样间隔秒数")
    refine.add_argument("--frame-selector", default=default_frame_selector(), help="画面筛选策略（默认 %(default)s）")
    refine.set_defaults(func=cmd_refine)

    doctor = sub.add_parser("doctor", help="检查依赖、模型、显卡与密钥")
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="显示配置文件位置，或用 --init 生成配置模板")
    config.add_argument("--init", action="store_true", help="在配置目录写入带注释的 config.toml 模板")
    config.add_argument("--force", action="store_true", help="与 --init 一起使用，覆盖已有文件")
    config.set_defaults(func=cmd_config)

    worker = sub.add_parser("worker", help="作为计算节点连接 Hub，领取转写、画面或整理任务")
    worker.add_argument("--hub", help="Hub 地址，例如 https://course.example.com")
    worker.add_argument("--token", help="节点令牌（也可用环境变量 COURSE2MD_WORKER_TOKEN）")
    worker.add_argument("--name", default="", help="节点名称，默认取主机名")
    worker.add_argument("--work-dir", help="工作目录，下载的视频与中间文件放在这里")
    worker.add_argument("--no-agents", action="store_true", help="不承担 DSH / Codex 整理任务")
    worker.add_argument("--capability", action="append", help="额外声明的能力，可重复")
    worker.add_argument("--without", action="append", help="不承担的能力，例如 --without asr，可重复")
    worker.set_defaults(func=cmd_worker)

    token = sub.add_parser("worker-token", help="在 Hub 所在电脑上为计算节点生成令牌")
    token.add_argument("--name", default="计算节点", help="节点名称")
    token.add_argument("--out-dir", help="Hub 的输出目录（数据库位于其中），默认取配置")
    token.set_defaults(func=cmd_worker_token)

    server = sub.add_parser("server", help="以服务器形态启动 Hub（需要登录、按用户隔离，通常放在 HTTPS 反向代理之后）")
    server.add_argument("--host", default="0.0.0.0")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--with-node", action="store_true", help="同时在本机启用内置计算节点（单机带算力的服务器使用）")
    server.set_defaults(func=cmd_server)

    web = sub.add_parser("web", help="启动本地图形界面")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    web.set_defaults(func=cmd_web)

    export = sub.add_parser("export", help="导出已有课程的 Anki 卡片")
    export.add_argument("source", help="包含 job.json 的课次输出目录")
    export.add_argument("--format", choices=["anki"], default="anki")
    export.add_argument("-o", "--out", required=True, help="TSV 输出文件")
    export.set_defaults(func=cmd_export)

    sync = sub.add_parser("sync", help="增量同步已有笔记到 Obsidian，保留本地修改")
    sync.add_argument("source", help="课次输出目录")
    sync.add_argument("--vault", required=True, help="Obsidian 仓库路径")
    sync.add_argument("--folder", default="course2md", help="vault 中的子目录")
    sync.set_defaults(func=cmd_sync)

    transfer = sub.add_parser("transfer", help="在多个 Hub 间互传课程产物，或离线导出/导入产物包")
    transfer_sub = transfer.add_subparsers(dest="transfer_action", required=True)

    t_list = transfer_sub.add_parser("list", help="列出指定 Hub 上可传输的课程与课次")
    t_list.add_argument("--hub", default="http://127.0.0.1:8765", help="Hub 地址（默认 %(default)s）")
    t_list.add_argument("--token", default="", help="API 令牌（服务器形态必填）")
    t_list.set_defaults(func=cmd_transfer)

    t_export = transfer_sub.add_parser("export", help="从 Hub 导出课程或课次产物包（.c2m.zip）")
    t_export.add_argument("--hub", default="http://127.0.0.1:8765", help="Hub 地址（默认 %(default)s）")
    t_export.add_argument("--token", default="", help="API 令牌")
    t_export.add_argument("--course", default="", help="要导出的课程编号")
    t_export.add_argument("--job", action="append", default=[], help="要导出的课次任务编号（可重复）")
    t_export.add_argument("-o", "--out", required=True, help="输出的 .c2m.zip 路径")
    t_export.add_argument(
        "--with-cache", action="store_true", help="同时带上各阶段缓存，导入后可从整理等中间阶段重新运行"
    )
    t_export.set_defaults(func=cmd_transfer)

    t_import = transfer_sub.add_parser("import", help="将产物包（.c2m.zip）导入指定 Hub")
    t_import.add_argument("bundle", help=".c2m.zip 产物包路径")
    t_import.add_argument("--hub", default="http://127.0.0.1:8765", help="目标 Hub 地址（默认 %(default)s）")
    t_import.add_argument("--token", default="", help="目标 Hub API 令牌")
    t_import.add_argument("--conflict", choices=["skip", "overwrite", "copy"], default="skip", help="冲突处理策略")
    t_import.set_defaults(func=cmd_transfer)

    t_copy = transfer_sub.add_parser("copy", help="将课程或课次产物从来源 Hub 直接传到目标 Hub")
    t_copy.add_argument("--from", dest="source_hub", default="http://127.0.0.1:8765", help="来源 Hub 地址")
    t_copy.add_argument("--from-token", dest="source_token", default="", help="来源 Hub API 令牌")
    t_copy.add_argument("--to", dest="target_hub", required=True, help="目标 Hub 地址")
    t_copy.add_argument("--to-token", dest="target_token", default="", help="目标 Hub API 令牌")
    t_copy.add_argument("--course", default="", help="要传输的课程编号")
    t_copy.add_argument("--job", action="append", default=[], help="要传输的课次任务编号（可重复）")
    t_copy.add_argument("--conflict", choices=["skip", "overwrite", "copy"], default="skip", help="冲突处理策略")
    t_copy.add_argument(
        "--with-cache", action="store_true", help="同时带上各阶段缓存，目标 Hub 可从整理等中间阶段重新运行"
    )
    t_copy.set_defaults(func=cmd_transfer)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]  # GBK 控制台无法显示 ✓ 等符号时替换，不中断
        except (AttributeError, ValueError):
            pass
    try:
        load_settings()
    except ConfigError as exc:
        print(f"配置文件有误：{exc}", file=sys.stderr)
        return 2
    bootstrap_runtime()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

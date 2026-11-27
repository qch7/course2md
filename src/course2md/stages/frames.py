"""画面阶段：全课扫描、筛选截图、写出索引。需要读取原视频，与媒体阶段在同一节点运行。"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from ..resolvers import ResolverError, download, get_resolver
from ..util.text import ensure_dir
from .artifacts import FrameSet, Transcript
from .base import BaseStage, StageContext

if TYPE_CHECKING:
    from .options import JobOptions


class FramesStage(BaseStage[list[Transcript], list[FrameSet]]):
    name: ClassVar[str] = "frames"
    label: ClassVar[str] = "画面"
    cache_options: ClassVar[tuple[str, ...] | None] = ("out_dir", "frame_interval", "frame_selector")
    pinned_to_anchor: ClassVar[bool] = True

    @classmethod
    def requires(cls, options: JobOptions, inputs: list[Transcript] | None = None) -> list[str]:
        from ..frame_selectors.api import DEFAULT_SELECTOR

        if options.frame_selector == DEFAULT_SELECTOR:
            return ["frames"]
        return ["frames:decode", f"frame-selector:{options.frame_selector}"]

    @classmethod
    def fallback_requires(cls, options: JobOptions) -> list[str]:
        """原视频可能已清理：其它节点需能解析来源重新下载。"""
        from .media import MediaStage

        source_caps = [cap for cap in MediaStage.requires(options) if cap != "asr"]
        return sorted(set(cls.requires(options) + source_caps))

    def run(self, ctx: StageContext, inputs: list[Transcript]) -> list[FrameSet]:
        from ..frames import sampling as frames_mod
        from ..refine.lesson import focus_timestamps

        options = ctx.options
        frame_dir = ensure_dir(options.out_dir / "frames")
        total = len(inputs)
        results: list[FrameSet] = []
        for index, item in enumerate(inputs, 1):
            ctx.check()
            base = (index - 1) / total
            span = 1.0 / total
            media_path = item.media.path
            if item.media.remote and not media_path.is_file():
                if not media_path.resolve().is_relative_to((options.out_dir / "media").resolve()):
                    raise ResolverError("视频路径不属于本任务，请从转写阶段重新运行")
                ctx.progress("画面", 0.56 + base * 0.02, "原视频已清理，正在重新下载", None)
                refs = get_resolver(options.source).resolve(options.source, options.session_id, options.views)
                ref = next((ref for ref in refs if ref.name == item.media.name and ref.view == item.media.view), None)
                if ref is None:
                    raise ResolverError("来源中找不到原机位，请更新来源链接或从转写阶段重新运行")
                download(ref, media_path, progress=lambda *_args: ctx.check())
                ctx.check()
            ctx.progress(
                "画面",
                0.56 + base * 0.02,
                f"扫描全课画面：{media_path.name}（每 {min(options.frame_interval, 1):g}s）",
                None,
            )
            frames = frames_mod.extract(
                media_path,
                frame_dir,
                interval=options.frame_interval,
                prefix=f"{item.stem}_",
                max_frames=None,
                focus_times=focus_timestamps(item.segments),
                selector_name=options.frame_selector,
                check_cancel=ctx.check,
                progress=_scaled(ctx, base, span),
            )
            index_path = None
            if frames:
                index_path = frames_mod.write_index(
                    frames, frame_dir / f"index-{item.stem}.md", title=f"{media_path.stem} 画面索引"
                )
            else:
                ctx.warn(f"{media_path.name} 抽不到画面帧，笔记将只依据转写稿")
            results.append(FrameSet(stem=item.stem, frames=frames, index=index_path))
        return results


def _scaled(ctx: StageContext, base: float, span: float):
    """把单路媒体的抽帧进度换算到整个任务的进度区间。"""

    def report(fraction: float, message: str) -> None:
        ctx.progress("画面", 0.56 + (base + fraction * span) * 0.02, message, fraction)

    return report

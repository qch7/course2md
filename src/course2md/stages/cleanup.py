"""成功后由原计算节点清理下载的视频。"""

from typing import ClassVar

from .artifacts import MediaOutput
from .base import BaseStage, StageContext


class CleanupStage(BaseStage[MediaOutput, None]):
    name: ClassVar[str] = "cleanup"
    label: ClassVar[str] = "清理视频"

    def run(self, ctx: StageContext, inputs: MediaOutput) -> None:
        media_dir = (ctx.options.out_dir / "media").resolve()
        for item in inputs.files:
            ctx.check()
            if item.remote and item.path.resolve().is_relative_to(media_dir):
                item.path.unlink(missing_ok=True)
        if media_dir.is_dir():
            try:
                media_dir.rmdir()
            except OSError:
                pass

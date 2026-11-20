"""接口请求体。

字段类型刻意放宽到「能被解析」的程度，具体取值由 :mod:`.service` 校验并给出中文提示，
这样网页收到的错误信息与旧接口一致。未列出的字段也会原样交给服务层（extra="allow"）。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(extra="allow")

    def payload(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


class ResolveRequest(_Body):
    source: str = Field("", description="课程链接或本地路径")
    out_dir: str | None = Field(None, description="输出根目录，留空使用配置中的默认值")
    term_start: str | None = Field(None, description="学期第 1 周起始日 YYYY-MM-DD")
    course_id: str | None = Field(None, description="已保存课程的编号，用于更新同一课程")


class RefreshRequest(_Body):
    term_start: str | None = None


class CreateJobRequest(_Body):
    source: str = Field("", description="课程链接或本地路径")
    session_ids: list[str] | None = Field(None, description="要整理的课次，每个课次一个任务")
    session_id: str | None = None
    course_id: str | None = None
    course: str | None = None
    title: str | None = None
    out_dir: str | None = None
    term_start: str | None = None
    formats: list[str] | str | None = Field(None, description="转写输出格式：srt、vtt、txt、md、json")
    views: list[int | str] | None = Field(None, description="机位编号")
    ai: bool | None = Field(None, description="是否进行 AI 校对与整理")
    proofread: bool | None = None
    detail: bool | None = Field(None, description="首选详细版笔记")
    frames: bool | None = Field(None, description="筛选课件画面")
    frame_interval: float | str | None = Field(None, description="画面采样间隔（秒）")
    frame_selector: str | None = Field(None, description="画面筛选策略：classroom、general 或已安装的插件名")
    device: str | None = Field(None, description="转写设备：cuda 或 cpu")
    model: str | None = Field(None, description="Whisper 模型")
    compute_type: str | None = None
    llm_base: str | None = Field(None, description="OpenAI 兼容接口地址")
    llm_model: str | None = None
    agent: str | None = Field(None, description="整理后端：dsh、codex、none 或自定义命令")
    agent_provider: str | None = None
    agent_model: str | None = None
    keep_audio: bool | None = None
    delete_video: bool | None = Field(None, description="成功后清理本任务下载的视频")
    overwrite: bool | None = Field(None, description="确认覆盖已有的整理结果")


class MessageRequest(_Body):
    text: str | None = Field(None, description="提问或修改要求")
    id: str | None = Field(None, description="客户端生成的请求编号，用于去重")


class RerunRequest(_Body):
    from_stage: str = Field("organize", description="从哪个阶段重新运行：media、frames、proofread、output、organize")
    agent: str | None = Field(None, description="换用的整理后端")
    agent_provider: str | None = None
    agent_model: str | None = None
    llm_model: str | None = Field(None, description="换用的内置整理 / 校对模型")
    detail: bool | None = None


class WorkerTokenRequest(_Body):
    name: str = Field("", description="节点名称，便于在计算服务页识别")
    personal: bool | None = Field(None, description="管理员创建只处理自己任务的个人节点；默认创建共享节点")


class PluginInstallRequest(_Body):
    source: str = Field(
        ..., min_length=1, max_length=4096, description="GitHub 仓库 HTTPS 链接或本机插件项目/安装包路径"
    )
    kind: str = Field("github", description="安装来源：github 或 local")

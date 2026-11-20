"""course2md：把课程回放转成结构化 Markdown 笔记。

流水线：链接解析 → 音频抽取 → 本地转写 → AI 整理成笔记。
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("course2md")
except PackageNotFoundError:  # 未安装、直接从源码目录运行
    __version__ = "0.0.0"

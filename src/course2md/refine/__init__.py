"""整理：校对转写稿，提取课程记录并渲染两版笔记。"""

from .llm import ChatClient, LlmError
from .proofread import ProofreadResult, proofread

__all__ = ["ChatClient", "LlmError", "ProofreadResult", "proofread"]

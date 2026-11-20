"""语音识别：音频准备、Whisper 后端、文本后处理与输出格式。"""

from .audio import SAMPLE_RATE, decode, normalize, prepare, read_wav, write_wav
from .filters import is_hallucination
from .formats import Paragraph, Segment, merge_paragraphs

__all__ = [
    "SAMPLE_RATE",
    "Paragraph",
    "Segment",
    "decode",
    "is_hallucination",
    "merge_paragraphs",
    "normalize",
    "prepare",
    "read_wav",
    "write_wav",
]

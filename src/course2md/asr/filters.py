"""转写文本的后处理：静音幻觉识别。

Whisper 在无人声窗口会吐出训练数据里的字幕套话；远场录音里真实讲话的
no_speech_prob 也会很高，所以这里只按文本模式判定，不看概率。
术语纠错交给整理阶段的大模型，见 refine/proofread.py。
"""

from __future__ import annotations

import re

# 训练数据里反复出现的“静音提示语”，命中即可丢弃
HALLUCINATION_PATTERNS = (
    r"请不吝.*?(点赞|订阅)",
    r"订阅.*?转发",
    r"打赏",
    r"明镜|点点栏目",
    r"字幕志愿者",
    r"字幕由.*?提供",
    r"感谢(您的)?观看",
    r"谢谢大家\s*[。.]?$",
    r"视频原声",
)
HALLUCINATION_RE = re.compile("|".join(HALLUCINATION_PATTERNS))


def is_hallucination(text: str) -> bool:
    """判断一句话是否是 Whisper 的静音幻觉。"""
    stripped = text.strip()
    if not stripped or not re.search(r"[\w\u4e00-\u9fff]", stripped):
        return True
    return bool(HALLUCINATION_RE.search(stripped))

"""OpenAI 兼容接口的最小客户端（DeepSeek / OpenAI / 本地 vLLM / Ollama 均可）。

注意：推理模型（如 deepseek-flash / reasoner）会先消耗思考 token，
`max_tokens` 太小时可见正文会被硬截断，所以这里检测 finish_reason=length 并自动加倍重试。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from typing import Any

import requests

from ..config import LlmConfig
from ..errors import Course2mdError

MAX_TOKEN_CEILING = 32000


class LlmError(Course2mdError):
    """调用大模型失败，消息面向用户可读。"""


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json_reply(text: str) -> Any:
    """从模型回复中取出 JSON 对象。

    Agent 常在 JSON 前后加一句说明并包进代码块（实测 DSH 约四分之一的读图回复如此），
    整段解析失败时依次尝试各个代码块，最后取第一个 ``{`` 到最后一个 ``}``。都失败时抛出 JSONDecodeError。
    """
    candidates = [text.strip(), *(block.strip() for block in _FENCE.findall(text))]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    error: json.JSONDecodeError | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            error = error or exc
    raise error or json.JSONDecodeError("回复中没有 JSON", text, 0)


class ChatClient:
    """只依赖 /chat/completions 的同步客户端，带重试。"""

    def __init__(self, cfg: LlmConfig) -> None:
        self.cfg = cfg
        self._key = cfg.resolved_key()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        return headers

    def chat(
        self, messages: Sequence[dict[str, object]], *, temperature: float | None = None, max_tokens: int | None = None
    ) -> str:
        """发一轮对话并返回助手文本。"""
        base = self.cfg.resolved_base_url()
        if not self._key and "localhost" not in base and "127.0.0.1" not in base:
            raise LlmError(
                "未配置大模型 API Key：请设置环境变量 DEEPSEEK_API_KEY（或 OPENAI_API_KEY），或在配置里传 --api-key"
            )

        url = base.rstrip("/") + "/chat/completions"
        budget = max_tokens or self.cfg.max_tokens
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": budget,
            "stream": False,
        }

        last_error = ""
        for attempt in range(1, self.cfg.retries + 1):
            try:
                resp = requests.post(url, headers=self._headers(), json=payload, timeout=self.cfg.timeout)
            except requests.RequestException as exc:
                last_error = f"网络错误：{exc}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    try:
                        choice = data["choices"][0]
                        text = choice["message"].get("content") or ""
                        finish = choice.get("finish_reason")
                    except (KeyError, IndexError) as exc:
                        raise LlmError(f"响应格式异常：{json.dumps(data)[:300]}") from exc
                    if finish == "length":
                        # 推理模型吃掉了预算：加倍重试，而不是把半截文本当结果
                        if budget < MAX_TOKEN_CEILING:
                            budget = min(budget * 2, MAX_TOKEN_CEILING)
                            payload["max_tokens"] = budget
                            last_error = f"输出被截断（finish_reason=length），已把预算提到 {budget}"
                            continue
                        raise LlmError(f"模型输出在 {budget} tokens 处被截断，请改用更小的分块或更强的模型")
                    return text
                if resp.status_code in (401, 403):
                    raise LlmError(f"鉴权失败（HTTP {resp.status_code}），请检查 API Key")
                last_error = f"HTTP {resp.status_code}：{resp.text[:200]}"
                if resp.status_code not in (408, 409, 429, 500, 502, 503, 504):
                    raise LlmError(last_error)
            time.sleep(min(2**attempt, 8))
        raise LlmError(f"重试 {self.cfg.retries} 次仍失败：{last_error}")

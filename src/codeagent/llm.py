"""LLM 客户端封装：OpenAI 兼容接口，统一走流式，自带瞬时错误重试。"""
import random
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from . import config

# 限流 / 断网 / 超时 / 服务端 5xx 属于瞬时错误，重试有意义；
# 4xx（鉴权失败、参数错误等）重试也不会成功，直接抛出。
RETRYABLE_ERRORS = (
    RateLimitError,
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
)


class LLMClient:
    def __init__(self):
        self.client = OpenAI(
            api_key=config.API_KEY,
            base_url=config.API_BASE,
            timeout=config.LLM_TIMEOUT,
        )

    def chat_stream(self, messages, tools=None):
        """发起流式对话请求，逐段 yield 模型输出。

        yield 的元素为 (kind, payload)：
          ("reasoning", 文本)  — 模型思考内容（如有），仅供展示
          ("content", 文本)    — 正文片段
          ("message", dict)    — 流结束时组装好的完整 assistant 消息

        瞬时错误按指数退避自动重试；失败前已输出过内容则不重试，
        避免把已打印的文本重放一遍。
        """
        kwargs = {"model": config.MODEL, "messages": messages, "stream": True}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": schema} for schema in tools
            ]

        for attempt in range(config.MAX_RETRIES + 1):
            emitted = False
            try:
                for event in self._stream_once(kwargs):
                    emitted = True
                    yield event
                return
            except RETRYABLE_ERRORS:
                if attempt == config.MAX_RETRIES or emitted:
                    raise
                wait = 2**attempt + random.random()
                print(
                    f"\n[LLM] 请求失败，{wait:.0f}s 后重试"
                    f"（{attempt + 1}/{config.MAX_RETRIES}）...",
                    flush=True,
                )
                time.sleep(wait)

    def _stream_once(self, kwargs):
        """消费一次流式响应：边 yield 增量边累积，最后 yield 完整消息。"""
        content_parts = []
        calls = {}  # 工具调用 index -> 累积中的 {"id", "name", "arguments"}

        for chunk in self.client.chat.completions.create(**kwargs):
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield ("reasoning", reasoning)
            if delta.content:
                content_parts.append(delta.content)
                yield ("content", delta.content)

            for tc in delta.tool_calls or []:
                idx = tc.index if tc.index is not None else 0
                slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] = tc.function.name
                if tc.function and tc.function.arguments:
                    slot["arguments"] += tc.function.arguments

        msg = {"role": "assistant", "content": "".join(content_parts)}
        if calls:
            msg["tool_calls"] = [
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"],
                    },
                }
                for _, slot in sorted(calls.items())
            ]
        yield ("message", msg)

"""LLM 客户端封装：OpenAI 兼容接口，统一走流式，自带瞬时错误重试。"""
import random
import time

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
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
          ("usage", dict)      — 流中携带的 token 用量（服务商支持才发）

        瞬时错误按指数退避自动重试；失败前已输出过内容则不重试，
        避免把已打印的文本重放一遍。
        """
        kwargs = {
            "model": config.MODEL,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
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
        """消费一次流式响应：边 yield 增量边累积，最后 yield 完整消息与用量。"""
        content_parts = []
        calls = {}  # 工具调用 index -> 累积中的 {"id", "name", "arguments"}
        latest_usage = None  # 有的服务商每个 chunk 都带 usage，始终记住最新一份

        for chunk in self._open_stream(kwargs):
            if getattr(chunk, "usage", None) is not None:
                latest_usage = _usage_to_dict(chunk.usage)
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
        if latest_usage is not None:
            yield ("usage", latest_usage)

    def _open_stream(self, kwargs):
        """发起流式请求；个别兼容服务不认识 stream_options 时自动降级重连。

        降级后只是拿不到用量，对话本身不受影响。
        """
        try:
            return self.client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            if "stream_options" in kwargs and "stream_options" in str(e):
                kwargs.pop("stream_options")
                return self.client.chat.completions.create(**kwargs)
            raise


def _usage_to_dict(usage):
    """把 SDK 的 usage 对象展平为普通 dict；结构异常时返回 None，绝不影响对话流。"""
    try:
        if isinstance(usage, dict):
            return dict(usage)
        return usage.model_dump()
    except Exception:  # noqa: BLE001
        return None

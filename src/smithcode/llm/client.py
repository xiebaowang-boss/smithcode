"""LLM 客户端封装：OpenAI 兼容接口，统一走流式，自带瞬时错误重试。"""
import random
import time
from contextlib import closing

from openai import (
    APIConnectionError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from .. import config, renderer
from ..cancel import current_token

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
        config.ensure_api_key()  # 凭证缺失时给出人话指引，别让 OpenAI SDK 抛裸异常
        self.client = OpenAI(
            api_key=config.KEY,
            base_url=config.URL,
            timeout=config.LLM_TIMEOUT,
        )
        self._custom_headers = config.load_provider_headers()

    def _resolved_headers(self) -> dict:
        """把配置的自定义请求头解析为实际值：{$session} 占位符替换为当前会话 id。

        每次请求现取现替换（而非构造时固化），保证 /new 轮换会话后仍发送新 id。
        """
        if not self._custom_headers:
            return {}
        return {
            name: value.replace("{$session}", config.SESSION_ID)
            for name, value in self._custom_headers.items()
        }

    def list_models(self):
        """拉取服务商可用模型列表（OpenAI 兼容 `GET /models`）。

        网络异常、接口不支持、返回为空一律返回 None——模型列表只是 `/model`
        的候选，缺失不应影响对话本身。返回去重后的模型 id 列表。
        """
        try:
            page = self.client.models.list()
        except Exception:  # noqa: BLE001
            return None
        names = []
        for item in page:
            name = getattr(item, "id", None)
            if isinstance(name, str) and name and name not in names:
                names.append(name)
        return names or None

    def chat_stream(self, messages, tools=None):
        """发起流式对话请求，逐段 yield 模型输出。

        yield 的元素为 (kind, payload)：
          ("reasoning", 文本)  — 模型思考内容（如有），仅供展示
          ("content", 文本)    — 正文片段
          ("message", dict)    — 流结束时组装好的完整 assistant 消息
          ("usage", dict)      — 流中携带的 token 用量（服务商支持才发）

        瞬时错误按指数退避自动重试；失败前已输出过内容则不重试，
        避免把已打印的文本重放一遍。任务被取消时（取消令牌已触发）流在
        下一块数据到达前截停并关闭 HTTP 连接，不再产出 message/usage，
        由 agent 侧拼装部分消息。
        """
        kwargs = {
            "model": config.MODEL,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if config.REASONING_EFFORT:
            kwargs["reasoning_effort"] = config.REASONING_EFFORT
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": schema} for schema in tools
            ]
        headers = self._resolved_headers()
        if headers:
            kwargs["extra_headers"] = headers  # 自定义请求头，_open_stream 重连时随 kwargs 沿用

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
                renderer.current().info(
                    f"\n[LLM] 请求失败，{wait:.0f}s 后重试"
                    f"（{attempt + 1}/{config.MAX_RETRIES}）..."
                )
                time.sleep(wait)

    def _stream_once(self, kwargs):
        """消费一次流式响应：边 yield 增量边累积，最后 yield 完整消息与用量。

        取消即时生效：打开流后把 `stream.close` 登记为令牌监听——取消线程
        直接关流，即使正阻塞在等待下一块数据（模型静默期 / 网络慢）也会
        立即解除阻塞（读抛错或迭代结束），不必等下一块到达。关流引发的
        读错误按取消处理（吞掉），非取消的真实异常照常上抛；任务被中断时
        不产出 message/usage，由 agent 侧拼装部分消息。
        """
        content_parts = []
        calls = {}  # 工具调用 index -> 累积中的 {"id", "name", "arguments"}
        latest_usage = None  # 有的服务商每个 chunk 都带 usage，始终记住最新一份
        token = current_token()
        if token is not None and token.cancelled:
            return  # 已取消：连新请求都不发起（避免中断后仍白跑一次调用）

        with closing(self._open_stream(kwargs)) as stream:
            if token is not None:
                token.subscribe(stream.close)  # 取消线程直接关流，解除阻塞中的读
            try:
                for chunk in stream:
                    if token is not None and token.cancelled:
                        return
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
            except Exception:
                if token is not None and token.cancelled:
                    return  # 关流引发的读错误：本质是取消，非真实异常
                raise

        if token is not None and token.cancelled:
            return  # 流恰好结束但已被取消：不产出 message/usage

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

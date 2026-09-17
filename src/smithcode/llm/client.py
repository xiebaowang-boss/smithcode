"""LLM 客户端封装：OpenAI 兼容接口，统一走流式，重试策略见 `llm/retry.py`。"""
from __future__ import annotations

import time
from contextlib import closing

from openai import BadRequestError, OpenAI

from .. import config, renderer
from ..cancel import current_token
from ..utils.proxy import normalize_proxy_env
from . import retry as retry_mod
from .retry import RetryPolicy, RetryState, classify


class LLMClient:
    def __init__(self):
        config.ensure_api_key()  # 凭证缺失时给出人话指引，别让 OpenAI SDK 抛裸异常
        normalize_proxy_env()  # 库被直接使用时（无 CLI 入口）同样兜底一次
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

    def chat_stream(self, messages, tools=None, model: str | None = None,
                    policy: RetryPolicy | None = None):
        """发起流式对话请求，逐段 yield 模型输出。

        model 非空时覆盖当前会话模型（会话标题等后台小请求用），默认
        使用 `config.MODEL`。policy 非空时覆盖重试策略（默认按 `config.MAX_RETRIES`）。

        yield 的元素为 (kind, payload)：
          ("reasoning", 文本)  — 模型思考内容（如有），仅供展示
          ("content", 文本)    — 正文片段
          ("message", dict)    — 流结束时组装好的完整 assistant 消息
          ("usage", dict)      — 流中携带的 token 用量（服务商支持才发）

        瞬时错误按 `llm.retry` 的策略自动重试（次数、退避、可重试判定都在那里），
        每次退避前上报 `retry_started` 供宿主显示进度。**正文已输出过也照常重试**：
        重放会重复打印已上屏的正文，但整轮报废的代价更大——上游（opencode /
        Codex / Claude Code）都是整请求重发，重复部分由上层按自己的消息模型
        处理（Agent 会把每次尝试的正文都记进同一条 assistant 消息）。思考内容
        只展示、不写入会话，流中断后可安全重算；工具调用在流结束前也不落历史。
        任务被取消时（取消令牌已触发）流在下一块数据到达前截停并关闭
        HTTP 连接，不再产出 message/usage，由 agent 侧拼装部分消息。
        """
        kwargs = {
            "model": model or config.MODEL,
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

        view = renderer.current()
        owner = self  # 重试态的归属者：前台任务与后台标题各自清理，互不误清
        policy = policy or RetryPolicy(max_attempts=config.MAX_RETRIES + 1)
        tail: dict = {}  # 最后一次尝试的 message / usage（循环正常结束才有效）

        try:
            for attempt_no in range(1, policy.max_attempts + 1):
                try:
                    # 增量边收边放：每次尝试都实时上屏（重试会把上一次的正文
                    # 再写一遍——上游同款行为，重复部分由 Agent 的消息模型处理）
                    for kind, payload in self._stream_once(kwargs):
                        if kind in ("content", "reasoning"):
                            yield kind, payload
                        else:
                            tail[kind] = payload  # 只认最后一次成功尝试的 message/usage
                    break
                except BaseException as e:  # 交策略判定是否值得重试
                    if not policy.should_retry(e, attempt_no):
                        raise
                    delay = policy.delay(attempt_no, e)
                    state = RetryState(
                        attempt=attempt_no + 1,
                        total=policy.max_attempts,
                        reason=classify(e),
                        wait=delay,
                        next_at=time.monotonic() + delay,
                    )
                    view.retry_started(state, owner)
                    retry_mod.wait(state)  # 退避期间可被 Esc 打断
        finally:
            view.retry_finished(owner)  # 成功或放弃都清掉重试态
        if "message" in tail:
            yield "message", tail["message"]
        if "usage" in tail:
            yield "usage", tail["usage"]

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

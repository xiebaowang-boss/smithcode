"""LLM 请求值对象：一次流式对话请求的不变快照。

`chat_stream(messages, tools, model)` 的老参数在此收成一个 frozen dataclass，
配套 `build_kwargs()` 纯函数负责组装 OpenAI SDK 的请求参数——不读任何全局
`config`，`default_model` 由调用方传入。请求"是什么"与"怎么发"从此分开，
`build_kwargs` 可脱离网络单测。

`TurnConfig` 是轮级快照：一轮 `run()` 里建 N 个 `ChatRequest`（每次 `_chat`
都现建一个，messages 本来每轮都在变），用它冻住"组装原料"——`run()` 开头
pin 一份，本轮内所有请求都用它，中途切配置只影响下一轮。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TurnConfig:
    """一轮任务的请求快照：模型与思考强度，`run()` 开头 pin、本轮内冻结。

    以后要冻 temperature / timeout 等加带默认值的字段即可：`capture()` 里读、
    `ChatRequest` 里透传，消费点不动。
    """

    model: str
    effort: str

    @classmethod
    def capture(cls, model_override: str | None = None) -> TurnConfig:
        from .. import config

        return cls(
            model=model_override or config.MODEL,
            effort=config.REASONING_EFFORT or config.DEFAULT_EFFORT,
        )


@dataclass(frozen=True)
class ChatRequest:
    """一次流式对话请求：消息、工具 schema、模型覆盖与请求头。"""

    messages: list
    tools: list | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    extra_headers: dict = field(default_factory=dict)


def build_kwargs(req: ChatRequest, *, default_model: str) -> dict:
    """把 `ChatRequest` 组装为 OpenAI SDK 的请求参数（纯函数，不读全局配置）。

    - model：请求级覆盖为空时回退 `default_model`；
    - tools：原始 schema 逐个包成 `{type: function, function: schema}`；
    - reasoning_effort / extra_headers：为空时不发送（服务商不支持会 400）。
    """
    kwargs = {
        "model": req.model or default_model,
        "messages": req.messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if req.reasoning_effort:
        kwargs["reasoning_effort"] = req.reasoning_effort
    if req.tools:
        kwargs["tools"] = [
            {"type": "function", "function": schema} for schema in req.tools
        ]
    if req.extra_headers:
        # 自定义请求头，`_open_stream` 降级重连时随 kwargs 沿用
        kwargs["extra_headers"] = dict(req.extra_headers)
    return kwargs

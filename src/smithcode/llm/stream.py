"""流式响应解析：SSE chunk 序列 → (kind, payload) 事件流（纯函数，无 IO）。

`_stream_once` 只剩接线（取消检查点、开流、订阅关闭），解析逻辑全部在此：
content / reasoning 增量透出、tool_calls 按 index 累积、usage 取最新有效值、
尾部组装完整 assistant 消息。输入是 chunk 对象即可单测，不再需要伪造整个流。
"""
from __future__ import annotations


def usage_to_dict(usage):
    """把 SDK 的 usage 对象展平为普通 dict；结构异常时返回 None，绝不影响对话流。"""
    try:
        if isinstance(usage, dict):
            return dict(usage)
        return usage.model_dump()
    except Exception:  # noqa: BLE001
        return None


def parse_stream(chunks):
    """消费一次流式响应的 chunk 序列：边 yield 增量边累积，最后产出完整消息与用量。

    yield 的事件与 `chat_stream` 同构：
      ("reasoning", 文本)  — 模型思考内容（如有），仅供展示
      ("content", 文本)    — 正文片段
      ("message", dict)    — 流结束时组装好的完整 assistant 消息
      ("usage", dict)      — 流中携带的 token 用量（服务商支持才发）

    tool_calls 按 `index` 槽位累积（`id` / `name` 覆盖、`arguments` 追加拼接），
    槽位按 index 排序后组装；空 index 视为 0。残缺槽位（空 id / 空 name）原样
    保留——是否可执行由 Agent 侧判定，这里只做累积、不丢信息。

    usage 取流中最后一份**有效**值：解析失败（`usage_to_dict` 返回 None）的
    包不覆盖之前已收到的有效值。
    """
    content_parts = []  # 仅用于 content 为空时的网关兼容判断，不组装 message
    calls = {}  # 工具调用 index -> 累积中的 {"id", "name", "arguments"}
    latest_usage = None  # 有的服务商每个 chunk 都带 usage，始终记住最新一份
    for chunk in chunks:
        if getattr(chunk, "usage", None) is not None:
            parsed = usage_to_dict(chunk.usage)
            if parsed is not None:
                latest_usage = parsed
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

    msg = {"role": "assistant", "content": ""}
    # content 刻意留空：正文以流式 content 事件为准（Agent 侧按尝试累积），
    # message 只交付 tool_calls 与归属——"正文是什么"只有一个来源。
    # `_complete` 的 `message and not parts` 兜底保留：某些网关零 content
    # 只有 message 时仍能拿到文本。
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

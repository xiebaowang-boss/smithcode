"""Agent 的错误类型。

自 `agent/agent.py` 搬出（纯搬运，语义一字不改）：这两个异常是**循环与宿主之间的
契约**（`StreamInterrupted` 决定 `stream_error` 状态、`SubscriberError` 决定"UI 故障
不当成网络中断"），放在自己的模块里，宿主与扩展引用它们时不必把整个 Agent 拉进来。
"""

from __future__ import annotations

from ..event.bus import SubscriberError


class StreamInterrupted(Exception):
    """模型响应流中途断开（读完超时 / 对端掐断连接）。

    `partial` 是流里已经收到的正文（可能为空字符串，即首块之前就断了）。
    已上屏的部分同时会被写进会话历史，保证「用户看到的」与「历史里的」一致；
    异常照常上抛，由 `run()` 转成 `stream_error` 状态交宿主渲染。
    """

    def __init__(self, original: BaseException, partial: str) -> None:
        super().__init__(f"{type(original).__name__}: {original}")
        self.original = original
        self.partial = partial


# 订阅者故障的类型在事件层定义（扇出在那里，见 `event/bus.py` 的说明）；
# 这里再导出一次，让 `smithcode.agent.SubscriberError` 这个既有引用点不变。
__all__ = ["StreamInterrupted", "SubscriberError"]

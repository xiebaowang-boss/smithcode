"""Agent 的错误类型。

自 `agent/agent.py` 搬出（纯搬运，语义一字不改）：这两个异常是**循环与宿主之间的
契约**（`StreamInterrupted` 决定 `stream_error` 状态），放在自己的模块里，宿主与扩展
引用它时不必把整个 Agent 拉进来。订阅者故障（`SubscriberError`）属于**事件层**
（扇出在那里，见 `event/bus.py`）——本模块不再转发它。
"""

from __future__ import annotations


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


__all__ = ["StreamInterrupted"]

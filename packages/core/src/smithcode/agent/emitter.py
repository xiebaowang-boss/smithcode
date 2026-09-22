"""事件发射通道（ContextVar）：让**不持有 Agent 的深层模块**也能发事件。

`Agent` 自己发事件走 `Agent.emit()`；但 `llm/client.py` 这类底层模块拿不到
Agent，它的重试进度（`StatusChanged(kind="retry")`）却是前端要的。这里用与
`signal.current_token()` 同形的办法解决：把「当前发射器」放进 ContextVar。

为什么不是给 `chat_stream()` 加参数：那个签名是**冻结接缝**——60 个测试假客户端
与扩展都在实现它，加一个可选参数会让所有旧实现报 `TypeError`。ContextVar 对
它们是透明的。

可见性：`asyncio.to_thread` 会复制当前上下文，所以跑在 worker 线程里的流式读取
（`drain_sync_stream`）也看得到同一个发射器。

没有通道时（单元测试直接调客户端、后台标题线程）返回 None，调用方退回原有的
渲染器直调路径——两条路径的**可观测结果一致**，只是通道不同。
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token

from ..event.catalog import AgentEvent

_emitter: ContextVar[Callable[[AgentEvent], None] | None] = ContextVar(
    "smithcode_emitter", default=None
)


def current() -> Callable[[AgentEvent], None] | None:
    """当前发射器；无则 None（调用方自行退回旧的渲染器直调）。"""
    return _emitter.get()


def emit(event: AgentEvent) -> bool:
    """尝试发一个事件，返回是否发出（False = 没有通道）。"""
    sink = _emitter.get()
    if sink is None:
        return False
    sink(event)
    return True


def activate(sink: Callable[[AgentEvent], None] | None) -> Token:
    """挂载发射器，返回复位令牌（与 `signal.activate_token` 同形）。"""
    return _emitter.set(sink)


def reset(token: Token) -> None:
    """按令牌复位（与 `activate` 配对；测试隔离也用它）。"""
    _emitter.reset(token)

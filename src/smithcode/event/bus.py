"""事件总线：进程内唯一的发布口与订阅口。

设计要点（对齐 opencode 的 `EventV2.publish` / `notify`，但按会话实例化）：

- **每会话一个总线实例**，总线自己带 `session_id`——所以「事件属于哪个会话」不是
  调用方手写出来的，而是发布时由总线注入。多客户端前端按会话订阅能拿到正确的
  标识（进程级单例做不到这件事）。
- **一个发布口**：`publish()`（模块函数）从当前上下文取总线；深层模块
  （权限引擎 / MCP / 技能 / llm 层）不必层层传参——与既有的
  `signal.activate_token` / `emitter.activate` 同一套 ContextVar 做法。
- **一个订阅口**：`Bus.subscribe()`（全体）与 `Bus.subscribe_type()`（按类型）。
- **投递语义**：按注册顺序同步调用，`emit` 负责把跨线程调用跳回事件循环线程
  （原 `Agent.emit` 的职责），订阅者异常**不吞**——订阅者自己的故障要暴露，
  不能被误判成模型流中断。

**线程与上下文（易踩）**：`publish()` 走 ContextVar，而
- `asyncio.to_thread` 会**复制**当前上下文 → worker 里也能用模块级 `publish()`（工具路径）；
- 裸 `threading.Thread` **不复制**上下文 → 那类线程（如后台标题线程）必须**持总线
  显式调用** `bus.publish(...)`，否则事件会静默消失。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from contextvars import ContextVar, Token

from . import envelope as _envelope
from .envelope import Envelope


class Bus:
    """一条会话的事件总线。"""

    def __init__(self, session_id: str | None = None) -> None:
        self.session_id = session_id
        self._listeners: list[Callable[[Envelope], None]] = []
        self._by_type: dict[str, list[Callable[[Envelope], None]]] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ----- 订阅 -----

    def subscribe(self, listener: Callable[[Envelope], None]) -> Callable[[], None]:
        """订阅全部事件，返回退订函数。"""
        with self._lock:
            self._listeners.append(listener)
        return lambda: self._unsubscribe(self._listeners, listener)

    def subscribe_type(self, type_name: str, listener: Callable[[Envelope], None]):
        """按类型名订阅（只收该类型），返回退订函数。"""
        with self._lock:
            self._by_type.setdefault(type_name, []).append(listener)
        return lambda: self._unsubscribe(self._by_type.get(type_name, []), listener)

    @staticmethod
    def _unsubscribe(bucket: list, listener) -> None:
        if listener in bucket:
            bucket.remove(listener)

    # ----- 发布 -----

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """记下本总线所属的事件循环，供跨线程发布跳转（worker 线程 / 后台标题线程）。"""
        self._loop = loop

    def publish(self, data: object, *, session_id: str | None = None) -> Envelope:
        """把领域载荷装进信封并投递。"""
        env = _envelope.wrap(
            data, session_id=self.session_id if session_id is None else session_id
        )
        self.emit(env)
        return env

    def emit(self, env: Envelope) -> None:
        """投递一个已装好信封的事件（线程安全）。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            self.deliver(env)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self.deliver(env)
            return
        loop.call_soon_threadsafe(self.deliver, env)

    def deliver(self, env: Envelope) -> None:
        """在事件循环线程上按注册顺序投递；订阅者异常不吞。

        调用方已确保处于循环线程时可直接用它（如 `Agent._emit` 同时要喂本轮流）。
        """
        with self._lock:
            targets = list(self._listeners) + list(self._by_type.get(env.type, ()))
        for listener in targets:
            listener(env)


# --------------------------------------------------------------------------
# 当前上下文的总线（ContextVar：并发会话各拿各的）
# --------------------------------------------------------------------------

_current: ContextVar[Bus | None] = ContextVar("smithcode_event_bus", default=None)


def current() -> Bus | None:
    """当前生效的总线；无总线时返回 None（纯单测直接调端口等场景）。"""
    return _current.get()


def activate(bus: Bus | None) -> Token:
    """挂载总线，返回复位令牌（与 `signal.activate_token` 同形）。"""
    return _current.set(bus)


def reset(token: Token) -> None:
    """按令牌复位（与 `activate` 配对；测试隔离也用它）。"""
    _current.reset(token)


def publish(data: object, *, session_id: str | None = None) -> Envelope | None:
    """向当前上下文的总线发布一个事件；没有总线时返回 None 且不报错。

    没有总线 = 调用方不在任何会话的运行上下文里（纯单测、无会话的命令路径）。
    此时静默丢弃是刻意的：事件是「告诉前端发生了什么」，没有前端就没有接收方，
    不该因为没人订阅而改变任何判定行为。
    """
    bus = _current.get()
    if bus is None:
        return None
    return bus.publish(data, session_id=session_id)

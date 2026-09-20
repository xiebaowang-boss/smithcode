"""有序事件流：`async for` 逐事件消费，`await result()` 取终结值。

对齐 pi 的 `EventStream`（`packages/ai/src/utils/event-stream.ts`）：消费者可以慢于
生产者（事件入队），也可以快于生产者（挂起等待）；终结事件由 `is_complete` 判定、
其值由 `extract` 取出。

与 pi 的差异（刻意，沿用既有判断）：**不在构造时创建 `asyncio.Future`**——`__init__`
可能在没有运行中事件循环的地方被调用，而 `asyncio.get_event_loop()` 在新版本 Python
上已不可依赖；故改为在 `result()` / 迭代内部（必然处于协程中）按需创建。

生产者异常：`end(error)` 后 `result()` 与迭代都会立即收尾，异常原样抛给调用方，
不静默截断。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Generic, TypeVar

from .catalog import AgentEnd
from .envelope import Envelope

E = TypeVar("E")
R = TypeVar("R")


class EventStream(Generic[E, R]):
    """有序事件流。"""

    def __init__(self, is_complete: Callable[[E], bool], extract: Callable[[E], R]) -> None:
        self._is_complete = is_complete
        self._extract = extract
        self._queue: deque[E] = deque()
        self._waiters: deque = deque()
        self._result_waiters: deque = deque()
        self._done = False
        self._has_result = False
        self._result: R | None = None
        self._error: BaseException | None = None

    @property
    def done(self) -> bool:
        """流是否已收尾（终结事件已发或生产者已 `end()`）。"""
        return self._done

    def push(self, event: E) -> None:
        """生产一个事件。终结事件会同时解析终结值；收尾后的事件被忽略。"""
        if self._done:
            return
        if self._is_complete(event):
            self._result = self._extract(event)
            self._has_result = True
            self._done = True
            self._wake_result_waiters()
        waiter = self._waiters.popleft() if self._waiters else None
        if waiter is not None:
            waiter.set_result(event)
        else:
            self._queue.append(event)

    def end(self, error: BaseException | None = None) -> None:
        """生产者收尾：唤醒全部等待者。error 非空表示生产者异常中断。"""
        self._done = True
        if error is not None:
            self._error = error
        while self._waiters:
            self._waiters.popleft().set_result(None)
        self._wake_result_waiters()

    def __aiter__(self) -> AsyncIterator[E]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[E]:
        while True:
            if self._queue:
                yield self._queue.popleft()
            elif self._done:
                return
            else:
                waiter: asyncio.Future = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                event = await waiter
                if event is None:  # end() 唤醒：不再有事件
                    return
                yield event

    async def result(self) -> R:
        """终结值。生产者异常中断时原样抛出该异常。"""
        if not self._has_result and not self._done:
            waiter = asyncio.get_running_loop().create_future()
            self._result_waiters.append(waiter)
            await waiter
        if self._has_result:
            return self._result  # type: ignore[return-value]
        if self._error is not None:
            raise self._error
        raise RuntimeError("事件流已结束，但没有产生终结事件")

    def _wake_result_waiters(self) -> None:
        while self._result_waiters:
            self._result_waiters.popleft().set_result(None)


def agent_event_stream() -> EventStream[Envelope, object]:
    """Agent run 的标准事件流：以 `AgentEnd`（信封里的载荷）终结，终结值为其 `result`。"""
    return EventStream(
        lambda env: isinstance(env.data, AgentEnd),
        lambda env: env.data.result,
    )

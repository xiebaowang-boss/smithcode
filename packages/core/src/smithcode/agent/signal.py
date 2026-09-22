"""协作式取消：`AbortSignal`。

**为什么不是 `asyncio.Task.cancel()`**（方案 §4 的论证，这里复述要点）：

1. pi 的核心也没有用异常解绕。它的 `AbortController` 每次 run 建一个
   （`agent.ts:501-511`），signal 显式穿参给 streamFn / `tool.execute` / 各钩子 /
   每个 `subscribe` 监听器，取消靠 `signal?.aborted` 的**状态判定**：流以
   `stopReason:"aborted"` 的最终消息编码（`agent-loop.ts:221-225`），工具产出
   `"Operation aborted"` 的错误结果（`:710-716`），清理靠一次性回调
   （`ctx.abortSignal?.addEventListener("abort", onAbort, { once: true })`）。
   等待用 `raceWithAbortSignal`（`ai/src/utils/abort.ts`）：放弃等待，但**继续
   观察被放弃的 promise**，避免 unhandled rejection。
2. smithcode 的中断语义与 task 强制取消冲突：要求「已确认的串行工具让它跑完、
   已预检未执行与剩余 `tool_calls` 一律补占位」，保证每个 `tool_call_id` 恰有
   一条结果。`CancelledError` 会穿透 `finally` / `gather` / `to_thread`，逼出
   成片的 `shield()` 与「占位是否写完」的时序问题。

所以保留原有的 token 模型（它与 pi 是同一个模型，且多了 ContextVar 传播），
只补齐 asyncio 下必需的三件能力：`wait()` / `race()` / `guard()`（外加
`on_abort()` 对应 pi 的 `addEventListener`）。

**并发约定**：`abort()` 可能来自任意线程（TUI 主线程调 `Agent.interrupt()`），
而 `wait()` / `race()` 跑在事件循环线程。跨线程唤醒走
`loop.call_soon_threadsafe`，登记与收集都在同一把锁内完成。
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from typing import Any, TypeVar

T = TypeVar("T")

# 未显式给出原因时的默认值。用户可见文案，保持与既有实现一致。
DEFAULT_ABORT_REASON = "用户中断"

#: 与文案并列的**机器可读**原因码：事件（`ExecutionInterrupted.reason`）按它分类，
#: 前端据此决定提示文案。取值对齐 opencode 的 interrupted.reason；新增来源
#: （进程退出 / 被新任务取代 / 空闲超时）时同步扩展这里。
DEFAULT_ABORT_CODE = "user"


class Cancelled(Exception):
    """`throw_if_aborted()` 在已中止时抛出。

    刻意只在**明确的检查点**抛（不是从任意 await 点穿透）：调用方想用协作式
    检查就用它，想自己分支就用 `aborted` 属性。
    """

    def __init__(self, reason: str = DEFAULT_ABORT_REASON) -> None:
        super().__init__(reason)
        self.reason = reason


class AbortSignal:
    """可跨线程中止、可在 asyncio 中等待的取消信号。

    同一实例是**一次性**的：一旦中止就永远保持中止状态（`abort()` 幂等）。
    """

    def __init__(self, code: str = DEFAULT_ABORT_CODE) -> None:
        self._event = threading.Event()
        self._code = code
        self._reason: str | None = None
        self._listeners: list[Callable[[], None]] = []
        # (loop, future)：abort 可能来自别的线程，必须经 call_soon_threadsafe 唤醒
        self._waiters: list[tuple[asyncio.AbstractEventLoop, asyncio.Future]] = []
        self._lock = threading.Lock()

    # ---------- 状态 ----------

    @property
    def aborted(self) -> bool:
        return self._event.is_set()

    # 既有名字，保留以便调用点零改动（阶段 3 起统一切到 aborted）
    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    # ---------- 发起中止 ----------

    @property
    def code(self) -> str:
        """机器可读的中止原因（`reason` 是给人看的文案）。"""
        return self._code

    def abort(self, reason: str = DEFAULT_ABORT_REASON,
              code: str | None = None) -> None:
        """请求中止（幂等、线程安全）：通知已登记的回调，并唤醒所有异步等待者。"""
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            if code is not None:
                self._code = code
            self._event.set()
            callbacks, self._listeners = self._listeners, []
            waiters, self._waiters = self._waiters, []
        for callback in callbacks:  # 锁外调用：回调里再取锁也不会死锁
            _safe_call(callback)
        for loop, future in waiters:
            _wake(loop, future)

    # 既有名字的别名（`Agent.interrupt()` 与测试沿用）
    def cancel(self, reason: str = DEFAULT_ABORT_REASON) -> None:
        self.abort(reason)

    # ---------- 登记与检查 ----------

    def subscribe(self, callback: Callable[[], None]) -> None:
        """登记中止回调（多次调用各自登记）。

        与 `on_abort` 的差别：不保证「已中止时立即触发」——调用点自己负责复查
        （如 `llm/client.py` 订阅后立刻查一次 `cancelled`）。
        """
        with self._lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[], None]) -> None:
        """摘除已登记的回调；找不到时静默（重复摘除是正常路径）。"""
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def on_abort(self, callback: Callable[[], None]) -> Callable[[], None]:
        """登记**一次性**中止回调，语义对齐 pi 的
        `addEventListener("abort", cb, { once: true })`：已中止时**立即调用一次**。

        返回退订函数（已立即调用过则退订为空操作）。
        """
        with self._lock:
            already = self._event.is_set()
            if not already:
                self._listeners.append(callback)
        if already:
            _safe_call(callback)
            return _noop
        return lambda: self.unsubscribe(callback)

    def throw_if_aborted(self) -> None:
        """检查点：已中止则抛 `Cancelled`（携带来时原因）。"""
        if self._event.is_set():
            raise Cancelled(self._reason or DEFAULT_ABORT_REASON)

    # ---------- asyncio ----------

    async def wait(self) -> None:
        """等到中止为止；已中止时立即返回。

        让「空闲等待」可被打断——不必再靠轮询或关流副作用。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        with self._lock:
            if self._event.is_set():  # 与 abort 竞争：锁内判定，不会漏唤醒
                return
            self._waiters.append((loop, future))
        await future

    def race(self, awaitable: Awaitable[T]) -> Awaitable[T]:
        """等 `awaitable`，但中止时**立刻放弃等待**（对齐 `raceWithAbortSignal`）。

        放弃后仍持续观察该 awaitable，异常与结果都会被取走，不会变成
        "coroutine was never awaited" 或 unhandled exception。
        """
        return _race_with_signal(awaitable, self)

    def guard(self, source: AsyncIterator[T]) -> AsyncIterator[T]:
        """给异步流加检查点：每次取下一块之前查一次中止。

        中止时静默结束该流（不抛），语义对齐既有的 `_iter_cancellable`——
        消费方按 `aborted` 判定中断。要显式抛出的检查点用 `throw_if_aborted()`。
        """
        return _guard_stream(source, self)


# --------------------------------------------------------------------------
# 当前轮次信号：ContextVar 传播
# --------------------------------------------------------------------------
#
# 显式参数是契约（工具与钩子能拿到 signal），ContextVar 兜住深层调用——
# `process.run` 自读当前信号以终止进程树、`mcp/connection` 轮询它决定是否
# 放弃等待。接口零侵入，跨线程入口（TUI / REPL 主线程）只调
# `Agent.interrupt()`，不接触信号本身。
#
# asyncio 下每个 Task 创建时复制一份上下文，因此子任务拿到的是创建时刻的
# 信号，互不串扰——比线程模型更干净。

_current: ContextVar[AbortSignal | None] = ContextVar("smithcode_abort_signal", default=None)


def current_token() -> AbortSignal | None:
    """当前线程/任务活跃轮次的取消信号；无任务运行时为 None。"""
    return _current.get()


def activate_token(signal: AbortSignal | None) -> Callable[[], None]:
    """激活信号并返回复位函数（`run()` 在 finally 中调用，恢复外层状态）。"""
    ticket = _current.set(signal)

    def reset() -> None:
        _current.reset(ticket)

    return reset


def _noop() -> None:
    """已立即调用过 `on_abort` 回调时的空退订函数。"""


def _safe_call(callback: Callable[[], None]) -> None:
    """回调失败不影响中止本身（与既有 `CancellationToken.cancel` 行为一致）。"""
    try:
        callback()
    except Exception:  # noqa: BLE001, S110 通知失败不该阻断取消
        pass


def _wake(loop: asyncio.AbstractEventLoop, future: asyncio.Future) -> None:
    """线程安全地唤醒一个等待者；循环已关闭时静默（收尾阶段属正常）。"""
    def settle() -> None:
        if not future.done():
            future.set_result(None)

    try:
        loop.call_soon_threadsafe(settle)
    except RuntimeError:  # 事件循环已关闭
        pass


async def _race_with_signal(awaitable: Awaitable[T], signal: AbortSignal) -> T:
    if signal.aborted:  # 快路径：连启动都不必（对齐 pi 的 signal.aborted 前置检查）
        _discard(awaitable)
        raise Cancelled(signal.reason or DEFAULT_ABORT_REASON)

    task = asyncio.ensure_future(awaitable)
    waiter = asyncio.ensure_future(signal.wait())
    try:
        done, _pending = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
    if task in done:
        return task.result()
    # 中止先到：放弃等待，但把该任务的结果/异常取走，别留 unhandled
    task.add_done_callback(_consume)
    raise Cancelled(signal.reason or DEFAULT_ABORT_REASON)


async def _guard_stream(source: AsyncIterator[T], signal: AbortSignal) -> AsyncIterator[T]:
    """逐块让出，每次拉取前查一次中止。

    中止时**静默停止**而不是抛 `Cancelled`——与既有同步检查点
    `llm/client.py:_iter_cancellable`（取消后 `return`，由调用方复查
    `cancelled` 决定产出）保持同一语义。这样它才是后者的直接替代：消费方
    看到流提前结束，按 `signal.aborted` 判定中断，不必在 `to_thread` 边界
    上再搬运一层异常。
    """
    try:
        while True:
            if signal.aborted:
                return
            try:
                item = await source.__anext__()
            except StopAsyncIteration:
                return
            yield item
    finally:
        aclose = getattr(source, "aclose", None)
        if callable(aclose):
            await aclose()


def _discard(awaitable: Awaitable[Any]) -> None:
    """丢弃一个从未启动的等待对象。

    协程可以 `close()`（否则解释器报 "coroutine was never awaited"）；已是
    Future / Task 的只能登记回调取走结果。
    """
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
        return
    if isinstance(awaitable, asyncio.Future):
        awaitable.add_done_callback(_consume)


def _consume(future: asyncio.Future) -> None:
    """取走被放弃任务的结果/异常，避免 "exception was never retrieved" 告警。"""
    if future.cancelled():
        return
    try:
        future.exception()
    except Exception:  # noqa: BLE001, S110 只是取走，不处理
        pass

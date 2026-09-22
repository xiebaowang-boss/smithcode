"""AbortSignal（agent/signal.py）：asyncio 侧的等待、竞速与流检查点。

重点验证两条**很难靠肉眼发现**的失效模式：

1. `race()` 放弃等待后被丢下的那个 awaitable，其结果/异常必须被取走——否则
   Task 析构时打 "Task exception was never retrieved"，或协程对象报
   "coroutine was never awaited"。二者都只在收尾时出现，不影响正确性判定，
   正因如此容易漏掉。
2. `abort()` 可能来自别的线程（TUI 主线程调 `Agent.interrupt()`），必须能
   唤醒事件循环里的等待者。

项目未安装 pytest-asyncio，故用 `asyncio.run` + 超时驱动，失败是断言失败而非挂死。
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import warnings

import pytest

from smithcode.agent.signal import AbortSignal, Cancelled, activate_token, current_token

TIMEOUT = 5.0


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), timeout=TIMEOUT))


# --------------------------------------------------------------------------
# 状态与检查点
# --------------------------------------------------------------------------


def test_abort_is_idempotent_and_keeps_the_first_reason():
    signal = AbortSignal()
    assert signal.aborted is False

    signal.abort("第一次")
    signal.abort("第二次")

    assert signal.aborted is True
    assert signal.reason == "第一次"


def test_legacy_names_still_work():
    """`cancelled` / `cancel` 是既有调用点在用的名字，语义必须一致。"""
    signal = AbortSignal()
    signal.cancel("旧名")
    assert signal.cancelled is True
    assert signal.reason == "旧名"


def test_throw_if_aborted_raises_with_reason():
    signal = AbortSignal()
    signal.throw_if_aborted()  # 未中止：不抛
    signal.abort("用户中断")
    with pytest.raises(Cancelled, match="用户中断"):
        signal.throw_if_aborted()


def test_subscribe_fires_on_abort_and_unsubscribe_is_silent_when_missing():
    signal = AbortSignal()
    fired: list[int] = []
    signal.subscribe(lambda: fired.append(1))
    signal.unsubscribe(lambda: None)  # 未登记过：静默
    signal.abort()
    assert fired == [1]


def test_on_abort_fires_immediately_when_already_aborted():
    signal = AbortSignal()
    signal.abort("早于登记")
    fired: list[int] = []
    unsubscribe = signal.on_abort(lambda: fired.append(1))

    assert fired == [1]  # 立即触发一次
    unsubscribe()  # 已触发过，退订为空操作


def test_on_abort_unsubscribe_prevents_the_call():
    signal = AbortSignal()
    fired: list[int] = []
    unsubscribe = signal.on_abort(lambda: fired.append(1))
    unsubscribe()
    signal.abort()
    assert fired == []


def test_listener_failure_does_not_block_abort():
    signal = AbortSignal()
    fired: list[int] = []

    def boom():
        raise RuntimeError("通知失败不该阻断取消")

    signal.on_abort(boom)
    signal.on_abort(lambda: fired.append(1))
    signal.abort()

    assert signal.aborted is True
    assert fired == [1]


# --------------------------------------------------------------------------
# asyncio：等待与竞速
# --------------------------------------------------------------------------


def test_wait_returns_immediately_when_already_aborted():
    async def scenario():
        signal = AbortSignal()
        signal.abort()
        await signal.wait()  # 不应挂起

    run(scenario)


def test_wait_is_woken_by_abort_from_another_thread():
    """跨线程唤醒：abort 由别的线程发起，等待者必须被叫醒。"""

    async def scenario():
        signal = AbortSignal()
        started = threading.Event()

        def abort_later():
            started.wait()
            time.sleep(0.05)
            signal.abort("来自其它线程")

        threading.Thread(target=abort_later, daemon=True).start()
        waiter = asyncio.ensure_future(signal.wait())
        started.set()
        await waiter
        return signal.reason

    assert run(scenario) == "来自其它线程"


def test_race_returns_the_result_when_the_operation_wins():
    async def scenario():
        signal = AbortSignal()

        async def fast():
            await asyncio.sleep(0)
            return "ok"

        return await signal.race(fast())

    assert run(scenario) == "ok"


def test_race_abandons_promptly_on_abort():
    """中止时立刻放弃等待，不等被等待的操作跑完。"""

    async def scenario():
        signal = AbortSignal()

        async def slow():
            await asyncio.sleep(1.0)
            return "太慢"

        def abort_later():
            time.sleep(0.05)
            signal.abort("用户中断")

        threading.Thread(target=abort_later, daemon=True).start()
        began = time.monotonic()
        with pytest.raises(Cancelled):
            await signal.race(slow())
        return time.monotonic() - began

    assert run(scenario) < 0.5


def test_race_on_already_aborted_signal_does_not_leave_a_pending_coroutine():
    """快路径：已中止时不启动操作，且不留下 "coroutine was never awaited"。"""

    async def scenario():
        signal = AbortSignal()
        signal.abort()

        async def never_started():
            raise AssertionError("不应被启动")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with pytest.raises(Cancelled):
                await signal.race(never_started())
            gc.collect()
        return [str(item.message) for item in caught]

    messages = run(scenario)
    assert not [text for text in messages if "never awaited" in text]


def test_race_consumes_the_exception_of_the_abandoned_operation():
    """被放弃的操作随后失败：异常必须被取走，而不是留给 Task 析构时上报。"""

    async def scenario():
        signal = AbortSignal()
        reported: list[str] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: reported.append(str(context.get("message"))))

        async def fails_late():
            await asyncio.sleep(0.05)
            raise ValueError("放弃之后才失败")

        waiter = asyncio.ensure_future(signal.race(fails_late()))
        await asyncio.sleep(0.01)
        signal.abort()
        with pytest.raises(Cancelled):
            await waiter

        await asyncio.sleep(0.15)  # 让被放弃的任务跑完并失败
        gc.collect()
        return reported

    reported = run(scenario)
    assert not [text for text in reported if "never retrieved" in text]


# --------------------------------------------------------------------------
# 流检查点
# --------------------------------------------------------------------------


def test_guard_stops_pulling_and_closes_the_source_after_abort():
    async def scenario():
        signal = AbortSignal()
        closed: list[int] = []

        async def source():
            try:
                for index in range(10):
                    yield index
            finally:
                closed.append(1)

        seen: list[int] = []
        async for item in signal.guard(source()):
            seen.append(item)
            if item == 1:
                signal.abort()
        return seen, closed

    seen, closed = run(scenario)
    assert seen == [0, 1]  # 中止后不再拉取
    assert closed == [1]  # 源被关闭，不留悬挂生成器


def test_guard_passes_through_when_never_aborted():
    async def scenario():
        signal = AbortSignal()

        async def source():
            for index in range(3):
                yield index

        return [item async for item in signal.guard(source())]

    assert run(scenario) == [0, 1, 2]


# --------------------------------------------------------------------------
# ContextVar 传播
# --------------------------------------------------------------------------


def test_current_signal_is_visible_inside_a_task_created_within_the_run():
    async def scenario():
        signal = AbortSignal()
        reset = activate_token(signal)
        try:
            return current_token() is signal
        finally:
            reset()

    assert run(scenario) is True

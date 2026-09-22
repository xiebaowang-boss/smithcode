"""同步生成器 → asyncio 的抽取（agent/stream_fn.py）。

要证明的两件事：
1. 抽取期间**事件循环没被阻塞**——否则整个异步改造白做（并发工具、TUI 响应都会卡住）；
2. 取消时不会留下滞留的工作线程——`asyncio.run` 退出会等待默认执行器收尾，
   滞留线程会让进程挂住。

项目未安装 pytest-asyncio，故用 `asyncio.run` + 超时驱动。
"""

from __future__ import annotations

import asyncio
import threading
import time

from smithcode.agent.signal import AbortSignal
from smithcode.agent.stream_fn import drain_sync_stream

TIMEOUT = 5.0


def run(scenario):
    return asyncio.run(asyncio.wait_for(scenario(), timeout=TIMEOUT))


def test_items_are_yielded_in_order_and_the_stream_ends():
    def source():
        yield ("content", "a")
        yield ("content", "b")
        yield ("message", {"role": "assistant"})

    async def scenario():
        return [item async for item in drain_sync_stream(source())]

    assert run(scenario) == [
        ("content", "a"),
        ("content", "b"),
        ("message", {"role": "assistant"}),
    ]


def test_draining_does_not_block_the_event_loop():
    """同步生成器在等网络时，事件循环必须还能跑别的任务。"""

    def slow_source():
        for index in range(3):
            time.sleep(0.05)
            yield ("content", str(index))

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        ticker_task = asyncio.ensure_future(ticker())
        try:
            items = [item async for item in drain_sync_stream(slow_source())]
        finally:
            ticker_task.cancel()
        return items, ticks

    items, ticks = run(scenario)
    assert [payload for _kind, payload in items] == ["0", "1", "2"]
    assert ticks > 5  # 被阻塞的循环不会有这么多 tick


def test_abort_stops_pulling_new_items():
    pulled: list[int] = []

    def source():
        for index in range(10):
            pulled.append(index)
            yield ("content", str(index))

    async def scenario():
        signal = AbortSignal()
        seen: list[str] = []
        async for _kind, payload in drain_sync_stream(source(), signal):
            seen.append(payload)
            if len(seen) == 2:
                signal.abort()
        return seen

    seen = run(scenario)
    assert seen == ["0", "1"]
    assert pulled == [0, 1]  # 中止后不再拉取第三块


def test_generator_error_propagates():
    def source():
        yield ("content", "a")
        raise ValueError("流中断")

    async def scenario():
        seen: list[str] = []
        try:
            async for _kind, payload in drain_sync_stream(source()):
                seen.append(payload)
        except ValueError as exc:
            return seen, str(exc)
        raise AssertionError("异常应当上抛")

    seen, message = run(scenario)
    assert seen == ["a"]
    assert message == "流中断"


def test_blocked_read_is_released_by_external_close():
    """模拟真实的取消路径：`next()` 阻塞在读取上，关流后立即解除，线程不滞留。

    这是 `llm/client.py` 的既有机制（取消时登记的回调关流，解除阻塞中的读），
    抽取层不必自己处理——但必须证明它确实不挂。

    在途的那一块**会被让出**：与异步化之前的同步 `for` 循环一致（它同样先拿到
    「取消那一刻已产出」的那一块再退出），不属于本次改造要改的行为。
    """
    release = threading.Event()

    def source():
        yield ("content", "first")
        release.wait(timeout=TIMEOUT)  # 模拟阻塞中的网络读
        yield ("content", "never delivered")

    async def scenario():
        signal = AbortSignal()
        seen: list[str] = []

        async def abort_and_release():
            await asyncio.sleep(0.05)
            signal.abort("用户中断")
            release.set()  # 等价于关流解除阻塞

        releaser = asyncio.ensure_future(abort_and_release())
        async for _kind, payload in drain_sync_stream(source(), signal):
            seen.append(payload)
        await releaser
        return seen

    assert run(scenario) == ["first", "never delivered"]

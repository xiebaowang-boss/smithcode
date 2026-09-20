"""核心事件流（event/stream.py）：有序交付、终结值、生产者异常的收尾语义。

项目未安装 pytest-asyncio（dev extra 只有 pytest / ruff），故用 `asyncio.run`
在同步用例里驱动协程，避免为测试引入新依赖。所有并发场景都带超时，失败是
断言失败而不是挂死。
"""

from __future__ import annotations

import asyncio
from typing import get_args

import pytest

from smithcode.cancel import RunResult
from smithcode.event import registry
from smithcode.event.catalog import (
    AgentEnd,
    AgentEvent,
    ExecutionStarted,
    MessageStart,
)
from smithcode.event.envelope import wrap
from smithcode.event.stream import EventStream, agent_event_stream

TIMEOUT = 5.0


def run(scenario):
    """跑一个协程场景；超时即判定为挂死。"""
    return asyncio.run(asyncio.wait_for(scenario(), timeout=TIMEOUT))


def test_events_are_delivered_in_push_order_and_terminal_event_ends_stream():
    async def scenario():
        stream: EventStream[str, str] = EventStream(
            is_complete=lambda event: event == "end",
            extract=lambda _event: "result",
        )
        stream.push("a")
        stream.push("b")
        stream.push("end")
        stream.push("late")  # 收尾后的事件被忽略

        seen = [event async for event in stream]
        return seen, await stream.result()

    seen, result = run(scenario)
    assert seen == ["a", "b", "end"]
    assert result == "result"


def test_slow_consumer_queues_events_and_fast_consumer_waits():
    async def collect(stream):
        return [event async for event in stream]

    async def scenario():
        stream: EventStream[str, None] = EventStream(
            is_complete=lambda _event: False,
            extract=lambda _event: None,
        )

        async def producer():
            for index in range(3):
                await asyncio.sleep(0)
                stream.push(f"e{index}")
            stream.end()

        consumer = asyncio.ensure_future(collect(stream))
        await producer()
        return await consumer

    assert run(scenario) == ["e0", "e1", "e2"]


def test_result_resolves_when_terminal_event_arrives_later():
    async def scenario():
        stream: EventStream[str, int] = EventStream(
            is_complete=lambda event: event == "end",
            extract=lambda _event: 42,
        )

        async def producer():
            await asyncio.sleep(0)
            stream.push("end")

        waiter = asyncio.ensure_future(stream.result())
        await producer()
        return await waiter

    assert run(scenario) == 42


def test_producer_error_propagates_instead_of_ending_silently():
    async def scenario():
        stream: EventStream[str, int] = EventStream(
            is_complete=lambda event: event == "end",
            extract=lambda _event: 1,
        )
        stream.push("a")
        stream.end(ValueError("boom"))

        seen = [event async for event in stream]
        with pytest.raises(ValueError, match="boom"):
            await stream.result()
        return seen

    assert run(scenario) == ["a"]


def test_producer_error_wakes_locked_consumer():
    """生产者崩溃必须唤醒等待者：消费者永久挂起比报错更糟。"""

    async def scenario():
        stream: EventStream[str, int] = EventStream(
            is_complete=lambda event: event == "end",
            extract=lambda _event: 1,
        )
        stream.push(MessageStart(message={"role": "user"}))

        async def crash():
            await asyncio.sleep(0)
            stream.end(RuntimeError("producer died"))

        task = asyncio.ensure_future(crash())
        seen = [event async for event in stream]
        await task
        return seen

    assert len(run(scenario)) == 1


def test_stream_without_terminal_event_reports_error():
    async def scenario():
        stream: EventStream[str, int] = EventStream(
            is_complete=lambda event: event == "end",
            extract=lambda _event: 1,
        )
        stream.end()
        return await stream.result()

    with pytest.raises(RuntimeError, match="没有产生终结事件"):
        run(scenario)


def test_agent_event_stream_terminates_on_agent_end():
    async def scenario():
        stream = agent_event_stream()
        stream.push(wrap(ExecutionStarted()))
        stream.push(wrap(AgentEnd(result=RunResult("ok", "正文"))))
        seen = [env.data async for env in stream]
        return seen, await stream.result()

    seen, result = run(scenario)
    # 流里走的是**信封**（订阅者与流看到同一个 id / 会话标识），载荷在 env.data
    assert [type(payload) for payload in seen] == [ExecutionStarted, AgentEnd]
    assert result.status == "ok"
    assert result.text == "正文"


def test_event_type_inventory_matches_union():
    """注册表是完备性断言的遍历依据，漏声明会让断言失去意义。"""
    declared = set(registry.declared_classes())
    # 联合里的每个成员都必须已声明（未声明 = 没有类型名，无法落盘/路由）
    for cls in get_args(AgentEvent):
        assert cls in declared, f"{cls.__name__} 未声明"

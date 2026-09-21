"""事件层（L0）单元测试：声明注册表 / 信封 / 总线 / 事件流。

这一层是全部后续阶段的底座，所以这里覆盖的是**契约**而不是实现细节：
类型名与版本是写进日志的稳定面、会话标识由总线注入、订阅是唯一入口、
无总线时静默丢弃（不因「没人订阅」而改变判定行为）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from smithcode.event import (
    Bus,
    EventStream,
    activate,
    agent_event_stream,
    catalog,
    current,
    new_id,
    payload_to_dict,
    publish,
    registry,
    reset,
    wrap,
)


@pytest.fixture
def bus():
    """干净的总线 + 挂上/复位上下文。"""
    b = Bus(session_id="sess-1")
    token = activate(b)
    yield b
    reset(token)


# ---------- 声明注册表 ----------


def test_every_catalog_event_is_declared():
    """目录里的每个事件类都必须有声明——漏了就没有类型名，也就无法落盘/路由。"""
    declared = set(registry.declared_classes())
    for cls in (
        catalog.MessageStart, catalog.MessageUpdate, catalog.MessageEnd,
        catalog.ToolStart, catalog.ToolPreview, catalog.ToolEnd,
        catalog.PlanUpdate, catalog.Notice, catalog.TitleChanged, catalog.AgentEnd,
        catalog.InboxEnqueued, catalog.InboxDelivered, catalog.InboxCancelled,
        catalog.InboxCleared, catalog.Idle, catalog.UsageChanged,
        catalog.CompactionStarted, catalog.CompactionEnded, catalog.CompactionFailed,
        catalog.ExecutionStarted, catalog.ExecutionSucceeded,
        catalog.ExecutionFailed, catalog.ExecutionInterrupted,
        catalog.StepStarted, catalog.StepEnded, catalog.StatusChanged,
        catalog.StatusCleared, catalog.PromptStarted, catalog.PromptFinished,
    ):
        assert cls in declared, f"{cls.__name__} 未声明"


def test_undeclared_class_has_no_meta():
    """未声明的类取 meta 必须报错，而不是构造出一个空 meta 蒙混过关。"""
    with pytest.raises(KeyError):
        registry.meta(dict)


def test_type_names_are_unique_per_version():
    """同类型名 + 同版本只能有一个类：两处争同一份日志契约要立刻失败。"""
    types = registry.declared_types()
    assert len(types) == len(set(types)), "类型名重复"


def test_version_resolution_prefers_latest():
    """按类型名取类时不给版本取最新；给了版本取不超过它的最新一个。"""

    @registry.declare("test.thing", version=1)
    class V1:
        pass

    @registry.declare("test.thing", version=2)
    class V2:
        pass

    assert registry.class_for("test.thing") is V2
    assert registry.class_for("test.thing", version=1) is V1
    assert registry.class_for("test.thing", version=2) is V2
    assert registry.class_for("test.thing", version=99) is V2
    assert registry.versioned_type("test.thing", 2) == "test.thing.2"


def test_duplicate_declaration_conflicts_are_rejected():
    """同名同版本换一个类 → 报错；同一个类重复声明 → 允许（幂等）。"""

    @registry.declare("test.clash", version=1)
    class A:
        pass

    with pytest.raises(ValueError):

        @registry.declare("test.clash", version=1)
        class B:
            pass

    registry.declare("test.clash", version=1)(A)  # 幂等，不抛


# ---------- 信封 ----------


def test_wrap_fills_identity_from_declaration():
    """信封的类型名/版本/持久性取自声明，id 与 created 自动生成。"""
    env = wrap(catalog.ToolStart(tool_call_id="t1", name="read_file", line="read a.py"),
         session_id="sess-9")
    assert env.type == "session.tool.started"
    assert env.version == 1
    assert env.durable is True  # 工具起止是持久骨架（可回放）
    assert env.session_id == "sess-9"
    assert env.seq is None
    assert env.id and len(env.id) == 32
    assert env.created <= time.time()


def test_payload_to_dict_is_json_shaped():
    """载荷 → 可 JSON 化的结构：dataclass 展开、tuple 变 list、嵌套项也跟着展开。"""
    env = wrap(catalog.InboxEnqueued(
        item=catalog.QueueItem(id="q1", text="继续", kind="steer")
    ))
    record = env.to_record()
    assert record["data"]["item"] == {"id": "q1", "text": "继续", "kind": "steer",
                                      "images": None, "created_at": 0.0}
    assert isinstance(payload_to_dict(catalog.Notice("hi")), dict)


def test_envelope_is_frozen():
    """信封与载荷都不可变——事件是已发生的事实，不允许被就地改写。"""
    env = wrap(catalog.Notice("hi"))
    with pytest.raises(FrozenInstanceError):
        env.session_id = "other"  # type: ignore[misc]


def test_new_id_is_unique():
    assert new_id() != new_id()


# ---------- 总线 ----------


def test_subscribe_receives_all_and_can_unsubscribe(bus):
    seen = []
    off = bus.subscribe(seen.append)
    publish(catalog.Notice("一"))
    off()
    publish(catalog.Notice("二"))
    assert [e.data.text for e in seen] == ["一"]


def test_subscribe_type_filters_by_type_name(bus):
    only_notice = []
    bus.subscribe_type("session.notice", only_notice.append)
    publish(catalog.Notice("通知"))
    publish(catalog.ExecutionStarted())
    assert [e.type for e in only_notice] == ["session.notice"]


def test_publish_injects_session_id_from_bus(bus):
    """会话标识由总线注入，调用方不手写——多客户端按会话路由的前提。"""
    seen = []
    bus.subscribe(seen.append)
    publish(catalog.ExecutionStarted())
    assert seen[0].session_id == "sess-1"


def test_publish_without_bus_is_silent():
    """无总线（纯单测 / 无会话路径）时静默丢弃：没有接收方不该改变判定行为。"""
    assert current() is None
    assert publish(catalog.Notice("没人收")) is None


def test_subscriber_exception_is_not_swallowed(bus):
    """订阅者自己的故障必须暴露（包成 `SubscriberError`），不能被静默掉。

    包装点在总线：扇出发生在这里，"谁炸了"也只有这里知道；散到各个发布方去包，
    漏掉一处就等于没有契约。
    """
    from smithcode.event.bus import SubscriberError

    def broken(_env):
        raise RuntimeError("前端炸了")

    bus.subscribe(broken)
    with pytest.raises(SubscriberError, match="前端炸了") as err:
        publish(catalog.Notice("x"))
    assert isinstance(err.value.__cause__, RuntimeError)  # 原始异常保留在 __cause__


def test_to_thread_publish_keeps_context_and_hops_to_loop():
    """工具经 `asyncio.to_thread` 下放：上下文被复制，模块级 publish() 仍能找到总线，
    且投递跳回事件循环线程（不是 worker 线程）。"""

    async def scenario():
        loop = asyncio.get_running_loop()
        b = Bus(session_id="s")
        b.bind_loop(loop)
        seen: list[tuple[str, str]] = []
        b.subscribe(lambda env: seen.append((env.data.text, threading.current_thread().name)))
        worker_thread: list[str] = []
        token = activate(b)
        try:
            def worker():
                worker_thread.append(threading.current_thread().name)
                publish(catalog.Notice("来自 worker"))

            await asyncio.wait_for(asyncio.to_thread(worker), timeout=5)
            for _ in range(50):
                await asyncio.sleep(0.01)
                if seen:
                    break
        finally:
            reset(token)
        return seen, worker_thread, threading.current_thread().name

    seen, worker_thread, loop_thread = asyncio.run(scenario())
    assert seen, "to_thread 里发布的事件必须被投递"
    assert seen[0][0] == "来自 worker"
    assert seen[0][1] == loop_thread  # 投递发生在循环线程
    assert seen[0][1] != worker_thread[0]


def test_raw_thread_must_use_explicit_bus():
    """裸 `threading.Thread` **不继承**上下文（与 to_thread 不同）。

    这类线程（如后台标题线程）拿不到模块级 `publish()`，必须持总线显式发布——
    这条规则写进 Bus 的文档，避免"事件在后台线程里静默消失"。
    """
    b = Bus(session_id="s")
    seen: list[str] = []
    b.subscribe(lambda env: seen.append(env.data.text))
    token = activate(b)  # 只在主线程上下文生效
    try:
        assert publish(catalog.Notice("主线程")) is not None
        from_worker = []
        thread = threading.Thread(
            target=lambda: from_worker.append(publish(catalog.Notice("worker"))), daemon=True
        )
        thread.start()
        thread.join(timeout=5)
    finally:
        reset(token)
    assert seen == ["主线程"]
    assert from_worker == [None], "裸线程里模块级 publish 找不到总线（返回 None）"
    # 显式持总线的做法可行
    b.publish(catalog.Notice("显式总线"))
    assert seen == ["主线程", "显式总线"]


# ---------- 事件流 ----------


def test_stream_delivers_in_order_and_extracts_result():
    """流按生产顺序消费；终结事件的载荷被抽成终结值。"""

    async def scenario():
        stream = agent_event_stream()
        stream.push(wrap(catalog.Notice("一")))
        stream.push(wrap(catalog.Notice("二")))
        stream.push(wrap(catalog.AgentEnd(result="RESULT")))
        types = [env.type async for env in stream]
        return types, await stream.result()

    types, result = asyncio.run(scenario())
    assert types == ["session.notice", "session.notice", "session.run.ended"]
    assert result == "RESULT"


def test_stream_iteration_returns_when_ended_without_result():
    """生产者收尾（无终结事件）时迭代正常结束，result() 报错而不是挂起。"""

    async def scenario():
        stream = EventStream(lambda _e: False, lambda _e: None)
        stream.push(object())
        stream.end()
        items = [item async for item in stream]
        return items

    assert len(asyncio.run(scenario())) == 1


def test_stream_results_producer_error():
    """生产者异常必须原样抛给消费者，不静默截断。"""

    async def scenario():
        stream = agent_event_stream()
        stream.end(RuntimeError("生产者炸了"))
        return await stream.result()

    with pytest.raises(RuntimeError, match="生产者炸了"):
        asyncio.run(scenario())


def test_envelope_round_trip_shape():
    """信封的最小字段集是稳定面：缺一个都会让回放/路由失效。"""
    env = wrap(catalog.TitleChanged(title="新标题"), session_id="s")
    assert set(env.to_record()) == {
        "id", "type", "version", "created", "session_id", "durable", "seq", "data",
    }

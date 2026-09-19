"""Agent 的事件出口：订阅、事件流、迁移桥、排队事件。

三件事要证明：
1. 核心发的类型化事件**顺序正确、能收口**（含异常路径），消费者不会挂住；
2. 迁移桥把事件翻回既有 Renderer 调用——前端因此一行不用改（阶段 5 才换订阅）；
3. 队列变更在**跨线程**（UI 线程入队、循环线程投递）下也安全：事件必须由
   事件循环线程发出，不能从 UI 线程直接 push。
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from smithcode import goal
from smithcode.agent import Agent
from smithcode.agent.events import (
    AgentEnd,
    QueueChanged,
    TurnEnd,
    TurnStart,
)
from smithcode.renderer import Renderer
from smithcode.session import Session

LIFECYCLE = (TurnStart, TurnEnd, AgentEnd)


@pytest.fixture(autouse=True)
def fresh_goal():
    goal.reset()
    yield
    goal.reset()


class ScriptedLLM:
    """按脚本逐次回放 assistant 消息（每次 chat_stream 消费一条）。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None):
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（脚本用尽）"}
        yield ("message", msg)


def _text(content):
    return {"role": "assistant", "content": content}


def _tool(name, args, call_id="1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


def _make_agent(monkeypatch, script=None) -> Agent:
    script = script if script is not None else [_text("最终回复")]
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(script))
    return Agent(session=Session())


def _names(events) -> list[str]:
    return [type(event).__name__ for event in events]


def _lifecycle(events) -> list:
    return [event for event in events if isinstance(event, LIFECYCLE)]


class RecordingRenderer(Renderer):
    """只记录收到的 Renderer 方法名。"""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def turn_started(self):
        self.calls.append("turn_started")

    def turn_finished(self, status="ok"):
        self.calls.append(f"turn_finished:{status}")

    def title_changed(self, title):
        self.calls.append(f"title_changed:{title}")


# ---------- 订阅与事件流 ----------


def test_subscriber_receives_run_lifecycle_in_order(monkeypatch):
    agent = _make_agent(monkeypatch)
    seen: list = []
    agent.subscribe(seen.append)

    asyncio.run(agent.run("打个招呼"))

    assert _names(_lifecycle(seen)) == ["TurnStart", "TurnEnd", "AgentEnd"]
    assert _lifecycle(seen)[-1].result.text == "最终回复"


def test_unsubscribe_stops_delivery(monkeypatch):
    agent = _make_agent(monkeypatch)
    seen: list = []
    unsubscribe = agent.subscribe(seen.append)

    unsubscribe()
    asyncio.run(agent.run("打个招呼"))

    assert seen == []


def test_event_stream_yields_events_and_terminal_result(monkeypatch):
    agent = _make_agent(monkeypatch)

    async def scenario():
        result = await agent.run("打个招呼")
        stream = agent.stream
        events = [event async for event in stream]
        return result, events, await stream.result()

    result, events, terminal = asyncio.run(scenario())

    # 视觉事件也在同一条流上：这一轮的假客户端只回一条完整消息（无增量），
    # 所以夹在 TurnStart / TurnEnd 之间的是 MessageEnd（正文块收口）
    assert _names(events) == ["TurnStart", "MessageEnd", "TurnEnd", "AgentEnd"]
    assert terminal is result


def test_each_task_opens_a_new_stream(monkeypatch):
    agent = _make_agent(monkeypatch, [_text("一"), _text("二")])

    asyncio.run(agent.run("第一条"))
    first = agent.stream
    asyncio.run(agent.run("第二条"))

    assert agent.stream is not first
    assert first.done is True


def test_stream_is_closed_with_error_when_loop_raises(monkeypatch):
    """循环抛未预期异常时事件流必须收口，否则消费者的 await 永远不返回。"""
    agent = _make_agent(monkeypatch)

    async def boom(_token):
        raise RuntimeError("循环炸了")

    monkeypatch.setattr(agent, "_run_loop", boom)

    async def scenario():
        with pytest.raises(RuntimeError, match="循环炸了"):
            await agent.run("打个招呼")
        return await agent.stream.result()

    with pytest.raises(RuntimeError, match="循环炸了"):
        asyncio.run(scenario())


def test_goal_driven_multi_turn_run_shares_one_stream(monkeypatch):
    """目标续跑连跑多轮：只有一条流、一个终结事件，终结值是最终结果。

    若每轮各自收口，消费者会在第一轮就拿到结果（pi 的 agent_end 是**整个 run**
    的终结事件，不是单轮的）。
    """
    agent = _make_agent(
        monkeypatch,
        [
            _tool("todo_write", {"todos": [{"title": "步骤一", "status": "in_progress"}]}, "1"),
            _text("第一步完成"),
            _tool("goal_update", {"status": "complete", "summary": "全部通过"}, "2"),
            _text("目标完成"),
        ],
    )
    goal.set("测试目标", max_turns=10)
    seen: list = []
    agent.subscribe(seen.append)

    async def scenario():
        return await agent.session_owner.run_with_goal("开始"), await agent.stream.result()

    result, terminal = asyncio.run(scenario())

    assert _names(_lifecycle(seen)).count("AgentEnd") == 1
    assert _names(_lifecycle(seen)).count("TurnStart") == 3  # 外层包装 + 两轮
    assert result.text == "目标完成"
    assert terminal is result


# ---------- 迁移桥：前端一行不改 ----------


def test_turn_events_still_reach_the_renderer(monkeypatch):
    agent = _make_agent(monkeypatch)
    recording = RecordingRenderer()
    monkeypatch.setattr("smithcode.agent.agent.renderer.current", lambda: recording)

    asyncio.run(agent.run("打个招呼"))

    assert recording.calls == ["turn_started", "turn_finished:ok"]


def test_title_change_reaches_the_renderer(monkeypatch):
    agent = _make_agent(monkeypatch)
    recording = RecordingRenderer()
    monkeypatch.setattr("smithcode.agent.agent.renderer.current", lambda: recording)

    assert agent.rename_session("新标题") is True

    assert recording.calls == ["title_changed:新标题"]


def test_bridge_follows_a_renderer_swap(monkeypatch):
    """宿主可能在 Agent 构造后才装上后端（TUI 即如此）：事件要发给当前后端。"""
    agent = _make_agent(monkeypatch)
    first, second = RecordingRenderer(), RecordingRenderer()

    monkeypatch.setattr("smithcode.agent.agent.renderer.current", lambda: first)
    agent.rename_session("一")
    monkeypatch.setattr("smithcode.agent.agent.renderer.current", lambda: second)
    agent.rename_session("二")

    assert first.calls == ["title_changed:一"]
    assert second.calls == ["title_changed:二"]


# ---------- 排队 ----------


def test_steer_emits_queue_changed_with_item_ids(monkeypatch):
    agent = _make_agent(monkeypatch)
    seen: list = []
    agent.subscribe(seen.append)

    item = agent.steer("先别改 config")

    assert isinstance(seen[-1], QueueChanged)
    assert [queued.id for queued in seen[-1].steering] == [item.id]
    assert seen[-1].follow_up == ()
    assert agent.pending_message_count == 1


def test_cancel_queued_removes_the_item_and_emits(monkeypatch):
    agent = _make_agent(monkeypatch)
    item = agent.steer("一")
    agent.steer("二")
    seen: list = []
    agent.subscribe(seen.append)

    assert agent.cancel_queued(item.id) is True

    assert [queued.text for queued in seen[-1].steering] == ["二"]
    assert agent.cancel_queued("不存在") is False


def test_follow_up_and_steering_are_separate_queues(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent.steer("插话")
    agent.follow_up("收尾")

    steering, follow_up = agent.clear_queue()

    assert steering == ["插话"]
    assert follow_up == ["收尾"]
    assert agent.pending_message_count == 0


def test_drain_follows_queue_mode(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent.steer("一")
    agent.steer("二")

    assert [item.text for item in agent.get_steering_messages()] == ["一"]

    agent.steering_queue.mode = "all"  # 抽水策略可切；此刻队列里还剩「二」
    agent.steer("三")
    agent.steer("四")

    assert [item.text for item in agent.get_steering_messages()] == ["二", "三", "四"]


class BlockingLLM:
    """第一块之后就卡住，给「运行中从另一个线程入队」留出时间窗。

    生成器跑在 `drain_sync_stream` 的 worker 线程里，故这里的阻塞只占住那个
    线程，事件循环照常转——这正是异步化的意义所在。
    """

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def chat_stream(self, messages, tools=None):
        yield ("content", "开始…")
        self.started.set()
        self.release.wait(5)
        yield ("message", {"role": "assistant", "content": "完成"})


def test_enqueue_from_ui_thread_emits_on_the_loop_thread(monkeypatch):
    """运行中从 UI 线程入队：事件由事件循环线程发出（不能跨线程碰事件流）。"""
    fake = BlockingLLM()
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: fake)
    agent = Agent(session=Session())
    seen: list[tuple[object, int]] = []
    agent.subscribe(lambda event: seen.append((event, threading.get_ident())))

    async def scenario():
        loop_ident = threading.get_ident()
        task = asyncio.ensure_future(agent.run("跑久一点"))
        assert await asyncio.to_thread(fake.started.wait, 5)  # 循环确实转起来了
        item = await asyncio.to_thread(agent.steer, "插一句")  # 模拟 UI 线程入队
        fake.release.set()
        await task
        return item, loop_ident

    item, loop_ident = asyncio.run(scenario())

    changes = [entry for entry in seen if isinstance(entry[0], QueueChanged)]
    assert [queued.id for queued in changes[-1][0].steering] == [item.id]
    assert changes[-1][1] == loop_ident  # 在循环线程上发出

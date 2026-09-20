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
from smithcode.event.catalog import (
    AgentEnd,
    QueueChanged,
    TitleChanged,
    TurnEnd,
    TurnStart,
)
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
    """载荷类型名序列（订阅者拿到的是信封，载荷在 env.data）。"""
    return [type(env.data).__name__ for env in events]


def _lifecycle(events) -> list:
    """只看回合/终结事件（返回信封，断言处用 .data 取载荷）。"""
    return [env for env in events if isinstance(env.data, LIFECYCLE)]


class RecordingSubscriber:
    """订阅者替身：记下收到的载荷类型名（事件只有一个通道，没有桥）。"""

    def __init__(self):
        self.calls: list[str] = []

    def __call__(self, env) -> None:
        data = env.data
        if isinstance(data, TurnStart):
            self.calls.append("turn_started")
        elif isinstance(data, TurnEnd):
            self.calls.append(f"turn_finished:{data.status}")
        elif isinstance(data, TitleChanged):
            self.calls.append(f"title_changed:{data.title}")


# ---------- 事件到达订阅者（无桥） ----------


def test_turn_events_reach_the_subscriber(monkeypatch):
    agent = _make_agent(monkeypatch)
    recording = RecordingSubscriber()
    agent.events.subscribe(recording)

    asyncio.run(agent.run("打个招呼"))

    assert recording.calls == ["turn_started", "turn_finished:ok"]


def test_title_change_reaches_the_subscriber(monkeypatch):
    agent = _make_agent(monkeypatch)
    recording = RecordingSubscriber()
    agent.events.subscribe(recording)

    assert agent.rename_session("新标题") is True

    assert recording.calls == ["title_changed:新标题"]


# ---------- 排队 ----------


def test_steer_emits_queue_changed_with_item_ids(monkeypatch):
    agent = _make_agent(monkeypatch)
    seen: list = []
    agent.events.subscribe(seen.append)

    item = agent.steer("先别改 config")

    assert isinstance(seen[-1].data, QueueChanged)
    assert [queued.id for queued in seen[-1].data.steering] == [item.id]
    assert seen[-1].data.follow_up == ()
    assert agent.pending_message_count == 1


def test_cancel_queued_removes_the_item_and_emits(monkeypatch):
    agent = _make_agent(monkeypatch)
    item = agent.steer("一")
    agent.steer("二")
    seen: list = []
    agent.events.subscribe(seen.append)

    assert agent.cancel_queued(item.id) is True

    assert [queued.text for queued in seen[-1].data.steering] == ["二"]
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
    agent.events.subscribe(lambda event: seen.append((event, threading.get_ident())))

    async def scenario():
        loop_ident = threading.get_ident()
        task = asyncio.ensure_future(agent.run("跑久一点"))
        assert await asyncio.to_thread(fake.started.wait, 5)  # 循环确实转起来了
        item = await asyncio.to_thread(agent.steer, "插一句")  # 模拟 UI 线程入队
        fake.release.set()
        await task
        return item, loop_ident

    item, loop_ident = asyncio.run(scenario())

    changes = [entry for entry in seen if isinstance(entry[0].data, QueueChanged)]
    assert [queued.id for queued in changes[-1][0].data.steering] == [item.id]
    assert changes[-1][1] == loop_ident  # 在循环线程上发出

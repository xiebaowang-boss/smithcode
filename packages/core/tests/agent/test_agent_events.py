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
    ExecutionStarted,
    ExecutionSucceeded,
    InboxCancelled,
    InboxEnqueued,
    StepEnded,
    StepStarted,
    TitleChanged,
)
from smithcode.session import Session

LIFECYCLE = (ExecutionStarted, ExecutionSucceeded, AgentEnd)


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
        if isinstance(data, ExecutionStarted):
            self.calls.append("turn_started")
        elif isinstance(data, ExecutionSucceeded):
            self.calls.append(f"turn_finished:{data.status}")
        elif isinstance(data, TitleChanged):
            self.calls.append(f"title_changed:{data.title}")


def test_execution_and_step_skeleton_in_a_real_run(monkeypatch):
    """真实一轮的事件骨架：execution 包着 N 个 step，步骤之间夹工具执行。

    这是「execution（一次 run）→ step（一次模型往返）」分层的验收断言：
    - 第 1 步产出工具调用（finish=tool_calls），工具执行夹在两步之间；
    - 第 2 步给出最终回复（finish=stop）；
    - 收尾是 ExecutionSucceeded，会话随后空闲（Idle）；
    - 每步带会话累计用量（UsageChanged 与模型调用一一对应）。
    """
    script = [
        _tool("read_file", {"path": "a.txt"}, "t1"),
        _text("读完了"),
    ]
    agent = _make_agent(monkeypatch, script)
    seen: list = []
    agent.events.subscribe(seen.append)

    asyncio.run(agent.run("打个招呼"))

    kinds = [env.type for env in seen]
    # 只保留骨架事件（流式增量/通知不参与断言）
    skeleton = [k for k in kinds if k in (
        "session.execution.started", "session.execution.succeeded",
        "session.step.started", "session.step.ended",
        "session.tool.started", "session.tool.ended",
        "session.usage.updated", "session.idle",
    )]
    assert skeleton == [
        "session.execution.started",
        "session.step.started",
        "session.step.ended",
        "session.usage.updated",   # 第 1 步的模型调用记账（步骤收口之后）
        "session.tool.started",
        "session.tool.ended",
        "session.step.started",    # 第 2 步：读到了工具结果
        "session.step.ended",
        "session.usage.updated",
        "session.execution.succeeded",
        "session.idle",
    ]
    steps = [env.data for env in seen if env.type.startswith("session.step.")]
    starts = [d for d in steps if isinstance(d, StepStarted)]
    ends = [d for d in steps if isinstance(d, StepEnded)]
    assert [d.index for d in starts] == [1, 2]  # 步序号从 1 起
    assert [d.finish for d in ends] == ["tool_calls", "stop"]
    assert [d.step_id for d in starts] == [d.step_id for d in ends]  # 起止配对
    # 用量事件与模型调用一一对应（假模型不回 usage，故只断言条数与配对）
    usages = [env.data for env in seen if env.type == "session.usage.updated"]
    assert len(usages) == 2
    assert [event.step.total_tokens for event in usages] == [0, 0]
    assert [event.calls for event in usages] == [0, 0]  # 假模型不回 usage：累计仍为 0
    assert any(env.type == "session.idle" for env in seen)  # 收尾回到空闲


def test_execution_interrupted_carries_the_reason(monkeypatch):
    """中断结局带原因（用户 / 进程退出 / 被取代）：前端据此决定提示文案。"""
    from smithcode.event.catalog import ExecutionInterrupted

    holder: dict = {}

    class InterruptingLLM:
        """流开始时按 Esc：模拟"用户在第一块之后中断"。"""

        def chat_stream(self, messages, tools=None):
            holder["agent"].interrupt()
            yield ("content", "开头")
            yield ("message", {"role": "assistant", "content": "开头"})

    monkeypatch.setattr("smithcode.agent.LLMClient", InterruptingLLM)
    agent = Agent(session=Session())
    holder["agent"] = agent
    seen: list = []
    agent.events.subscribe(seen.append)

    result = asyncio.run(agent.run("跑起来"))

    assert result.status == "interrupted"
    endings = [env.data for env in seen if isinstance(env.data, ExecutionInterrupted)]
    assert len(endings) == 1
    assert endings[0].reason == "user"
    # 中断也要收口步骤（不能留下"还在跑"的一步）
    assert [env.type for env in seen].count("session.step.started") == sum(
        1 for env in seen if env.type == "session.step.ended"
    )


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


def test_steer_emits_inbox_enqueued_with_the_item(monkeypatch):
    agent = _make_agent(monkeypatch)
    seen: list = []
    agent.events.subscribe(seen.append)

    item = agent.steer("先别改 config")

    assert isinstance(seen[-1].data, InboxEnqueued)
    assert seen[-1].data.item.id == item.id  # 事件带完整项（前端不必再拉快照）
    assert agent.pending_message_count == 1


def test_cancel_queued_removes_the_item_and_emits(monkeypatch):
    agent = _make_agent(monkeypatch)
    item = agent.steer("一")
    agent.steer("二")
    seen: list = []
    agent.events.subscribe(seen.append)

    assert agent.cancel_queued(item.id) is True

    assert isinstance(seen[-1].data, InboxCancelled)
    assert seen[-1].data.item_id == item.id  # 离队的是哪一条，事件里说得清
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

    changes = [entry for entry in seen if isinstance(entry[0].data, InboxEnqueued)]
    assert [entry[0].data.item.id for entry in changes] == [item.id]
    assert changes[-1][1] == loop_ident  # 在循环线程上发出

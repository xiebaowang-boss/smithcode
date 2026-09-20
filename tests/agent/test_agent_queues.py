"""排队接进循环：三个抽水点（起点 / 每轮末 / 本要停时）真的会投递。

队列本身（增删清投、id 配对、抽水策略）在 test_queues.py；这里只验「循环什么时候
把它取走、取走后作为什么进历史」，以及投递后队列确实空、事件确实发了。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode.agent import Agent
from smithcode.event.catalog import InboxDelivered
from smithcode.session import Session


def _tool(name="list_dir", args=None, call_id="1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args or {})},
            }
        ],
    }


def _text(content):
    return {"role": "assistant", "content": content}


def _register_fake_tool(monkeypatch, executed: list[str]) -> None:
    """假工具 + 放行权限（聚焦排队语义，避开真实权限规则）。"""
    from smithcode.permission import Permission
    from smithcode.tools import FUNCTIONS

    def fake(path=None):
        executed.append(path or "")
        return f"已执行 {path}"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", fake)
    monkeypatch.setattr(Permission, "check", lambda self, *a, **k: True)
    monkeypatch.setattr(Permission, "check_paths", lambda self, *a, **k: True)


class SteeringLLM:
    """可选的「第一轮调用时插话」（模拟用户在任务运行中提交输入）。"""

    def __init__(self, holder: dict, script: list, steer_on_first_call: str | None = None):
        self.holder = holder
        self.script = list(script)
        self.steer_on_first_call = steer_on_first_call
        self.calls = 0
        self.seen: list[list] = []

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        self.seen.append([m.get("content", "") for m in messages])
        if self.calls == 1 and self.steer_on_first_call is not None:
            self.holder["agent"].steer(self.steer_on_first_call)
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（用尽）"}
        yield ("message", msg)


def _agent(monkeypatch, llm) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: llm)
    return Agent(session=Session())


def test_steering_is_delivered_after_the_current_turn(monkeypatch):
    """插话不打断当前轮：本轮工具跑完，下一轮模型调用才看到它。"""
    executed: list[str] = []
    _register_fake_tool(monkeypatch, executed)
    holder: dict = {}
    llm = SteeringLLM(
        holder, [_tool("fake_tool", {"path": "a"}), _text("做完了")],
        steer_on_first_call="顺便看下 README",
    )
    agent = _agent(monkeypatch, llm)
    holder["agent"] = agent
    seen_events: list = []
    agent.events.subscribe(seen_events.append)

    result = asyncio.run(agent.run("做事"))

    assert result.status == "ok"
    assert executed == ["a"]  # 本轮工具照常执行
    assert llm.calls == 2  # 插话换来一次续跑
    assert "顺便看下 README" not in llm.seen[0]  # 第一轮看不到（还没插）
    assert "顺便看下 README" in llm.seen[1]  # 第二轮看得到
    assert agent.steering_queue.count == 0  # 投递即出队
    delivered = [env.data for env in seen_events if isinstance(env.data, InboxDelivered)]
    assert [event.item.text for event in delivered] == ["顺便看下 README"]  # 投递发了事件


def test_follow_up_continues_instead_of_stopping(monkeypatch):
    """本要停时还有 follow-up：接着跑，而不是先把这轮结束掉。"""
    holder: dict = {}
    llm = SteeringLLM(holder, [_text("第一轮回复"), _text("收尾回复")])
    agent = _agent(monkeypatch, llm)
    holder["agent"] = agent
    # 直接入队（不经过运行中提交）：等价于「用户在模型给出最终回复前提交」
    agent.follow_up("还有一件事")
    agent.steering_queue.clear()  # 只留 follow-up

    result = asyncio.run(agent.run("做事"))

    assert llm.calls == 2
    assert result.text == "收尾回复"
    assert "还有一件事" in llm.seen[1]
    assert agent.follow_up_queue.count == 0


def test_steering_is_delivered_at_the_start_of_the_run(monkeypatch):
    """抽水点 1：上一次等待期间提交的插话，在本次 run 的第一轮就能看到。"""
    holder: dict = {}
    llm = SteeringLLM(holder, [_text("收到")])
    agent = _agent(monkeypatch, llm)
    holder["agent"] = agent
    agent.steer("先看这个")

    asyncio.run(agent.run("做事"))

    assert "先看这个" in llm.seen[0]
    assert agent.steering_queue.count == 0


# ---------- 配置驱动的投递方式（[queue] delivery） ----------


def _queue_config(monkeypatch, **values) -> None:
    monkeypatch.setattr("smithcode.config._read_config_file", lambda: {"queue": values})


def test_queue_config_defaults_to_follow(monkeypatch):
    """未配置时：运行中提交默认 follow（等本轮跑完再送），不需要任何新按键。"""
    from smithcode import config

    _queue_config(monkeypatch)

    assert config.load_queue_config().delivery == "follow"


def test_prompt_enqueues_as_follow_while_busy(monkeypatch):
    from smithcode import config

    _queue_config(monkeypatch, delivery="follow")
    agent = _agent(monkeypatch, SteeringLLM({}, [_text("x")]))
    agent._token = object()  # 假装正在运行（prompt 只看这个）

    assert asyncio.run(agent.prompt("排队一条")) is None
    assert [item.text for item in agent.follow_up_queue.list()] == ["排队一条"]
    assert agent.steering_queue.count == 0
    assert config.load_queue_config().delivery == "follow"


def test_prompt_enqueues_as_steer_when_configured(monkeypatch):
    _queue_config(monkeypatch, delivery="steer")
    agent = _agent(monkeypatch, SteeringLLM({}, [_text("x")]))
    agent._token = object()

    assert asyncio.run(agent.prompt("插一条")) is None
    assert [item.text for item in agent.steering_queue.list()] == ["插一条"]


def test_prompt_runs_when_idle(monkeypatch):
    """空闲：直接开跑（配置不参与），返回本轮结果。"""
    _queue_config(monkeypatch, delivery="steer")
    holder: dict = {}
    agent = _agent(monkeypatch, SteeringLLM(holder, [_text("跑完了")]))
    holder["agent"] = agent

    result = asyncio.run(agent.prompt("做事"))

    assert result.status == "ok"
    assert result.text == "跑完了"
    assert agent.pending_message_count == 0


def test_prompt_rejects_unknown_delivery(monkeypatch):
    agent = _agent(monkeypatch, SteeringLLM({}, [_text("x")]))
    agent._token = object()

    with pytest.raises(ValueError):
        asyncio.run(agent.prompt("做事", delivery="不行"))


def test_queue_modes_come_from_config(monkeypatch):
    _queue_config(monkeypatch, steering_mode="all", follow_up_mode="one-at-a-time")
    agent = _agent(monkeypatch, SteeringLLM({}, [_text("x")]))

    assert agent.steering_queue.mode == "all"
    assert agent.follow_up_queue.mode == "one-at-a-time"


def test_queue_mode_one_at_a_time_delivers_one_per_turn(monkeypatch):
    """抽水策略生效：one-at-a-time 每次只投一条，剩下的留到下一轮。"""
    executed: list[str] = []
    _register_fake_tool(monkeypatch, executed)
    holder: dict = {}
    llm = SteeringLLM(holder, [_tool("fake_tool", {"path": "a"}), _text("做完了")])
    agent = _agent(monkeypatch, llm)
    holder["agent"] = agent
    agent.steer("插话一")
    agent.steer("插话二")

    asyncio.run(agent.run("做事"))

    assert "插话一" in llm.seen[0]
    assert "插话二" not in llm.seen[0]  # 一次只投一条
    assert "插话一" in llm.seen[1] and "插话二" in llm.seen[1]  # 第二条在下一轮投递
    assert agent.steering_queue.count == 0

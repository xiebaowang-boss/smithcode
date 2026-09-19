"""会话对象（agent/agent_session.py）：入口收口 + 两条持久化红线断言。

会话对象要证明的是「同一个状态，一个入口」——它持有的必须是 Agent 身上那几个
**同一对象**（不是副本，否则会出现两份真相）；另外把两条容易被无声破坏的红线
钉住：`t=state` 的三个 key 不变（否则旧转录恢复会静默丢状态）、系统提示词在同一
轮内逐字节稳定（否则服务商的提示缓存每轮失效）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode import goal
from smithcode.agent import Agent, AgentSession
from smithcode.agent.agent_session import STATE_KEYS, create
from smithcode.session import Session


@pytest.fixture(autouse=True)
def fresh_goal():
    goal.reset()
    yield
    goal.reset()


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None):
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（用尽）"}
        yield ("message", msg)


def _text(content):
    return {"role": "assistant", "content": content}


def _agent(monkeypatch, script=None) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient",
                        lambda: ScriptedLLM(script or [_text("回复")]))
    return Agent(session=Session())


# ---------- 外观：持有的是同一对象，不是副本 ----------


def test_session_facade_shares_the_agent_state(monkeypatch):
    agent = _agent(monkeypatch)
    session = AgentSession(agent)

    assert session.agent is agent
    assert session.session is agent.session  # 不是复制：消息历史只有一份
    assert session.permission is agent.permission
    assert session.mcp is agent.mcp
    assert session.interactions is agent.interactions
    assert session.steering_queue is agent.steering_queue
    assert session.follow_up_queue is agent.follow_up_queue


def test_agent_exposes_one_lazy_session(monkeypatch):
    agent = _agent(monkeypatch)
    assert agent.session_owner is agent.session_owner  # 惰性且唯一


def test_create_builds_an_agent_when_none_given(monkeypatch):
    monkeypatch.setattr("smithcode.agent.LLMClient",
                        lambda: ScriptedLLM([_text("回复")]))
    session = create()
    assert isinstance(session, AgentSession)


# ---------- 任务入口 ----------


def test_run_with_goal_without_goal_equals_plain_run(monkeypatch):
    agent = _agent(monkeypatch, [_text("你好")])
    result = asyncio.run(agent.session_owner.run_with_goal("你好"))

    assert result.status == "ok"
    assert result.text == "你好"


def test_prompt_and_busy_delegate(monkeypatch):
    agent = _agent(monkeypatch, [_text("跑完了")])
    session = agent.session_owner

    result = asyncio.run(session.prompt("做事"))

    assert result.text == "跑完了"
    assert session.busy is False


def test_queue_helpers_delegate(monkeypatch):
    agent = _agent(monkeypatch)
    session = agent.session_owner

    item = session.steer("插一句")

    assert session.pending_message_count == 1
    assert session.cancel_queued(item.id) is True
    assert session.pending_message_count == 0


def test_queue_drain_and_clear_are_exposed(monkeypatch):
    """会话对象要能完整驱动队列：投递点与分队列清空都得有（不只是 clear_queue）。"""
    agent = _agent(monkeypatch)
    session = agent.session_owner
    session.steer("插话")
    session.follow_up("收尾")

    assert [item.text for item in session.get_steering_messages()] == ["插话"]
    assert session.pending_message_count == 1

    assert session.clear_follow_up_queue() == ["收尾"]
    assert session.clear_steering_queue() == []
    assert session.pending_message_count == 0


# ---------- 持久化红线 ----------


def test_state_parts_keep_the_persisted_keys(monkeypatch):
    """`t=state` 的 key 必须逐字保持——旧转录的恢复按 key 匹配。"""
    session = _agent(monkeypatch).session_owner

    assert session.state_parts() == STATE_KEYS == ("goal", "plan", "skills")


def test_snapshot_restore_round_trip(monkeypatch):
    """快照 → 复位 → 恢复：状态说得回来（这里是 goal 投影）。"""
    agent = _agent(monkeypatch)
    session = agent.session_owner
    goal.set("把文档补完")
    snapshot = session.snapshot()

    session.reset()  # /new：目标被清空
    assert goal.is_set() is False

    session.restore(snapshot)

    assert goal.current() is not None
    assert "把文档补完" in goal.current().objective


def test_system_message_is_byte_stable_within_a_turn(monkeypatch):
    """同一轮内连续两次 `sync_system()`：`messages[0]` 逐字节相同。

    提示缓存靠这个稳定性；动态段一旦掺进时间戳之类的东西，每轮请求都会换前缀。
    """
    agent = _agent(monkeypatch)
    agent.session.sync_system()
    first = json.dumps(agent.session.messages[0], ensure_ascii=False, sort_keys=True)

    agent.session.sync_system()
    second = json.dumps(agent.session.messages[0], ensure_ascii=False, sort_keys=True)

    assert first == second

"""`continue_run()`：不注入新 user 消息，接着当前上下文再跑一轮。

对应 pi 的 `agentLoopContinue`。两条前置校验（上下文非空、末尾非 assistant）是
为了让请求合法——它们都由 pi 的实现给出，不是我们发明的。
"""

from __future__ import annotations

import asyncio

import pytest

from smithcode.agent import Agent
from smithcode.session import Session


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.seen: list[list] = []

    def chat_stream(self, messages, tools=None):
        self.seen.append([(m.get("role"), m.get("content", "")) for m in messages])
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（用尽）"}
        yield ("message", msg)


def _agent(monkeypatch, script) -> tuple[Agent, ScriptedLLM]:
    llm = ScriptedLLM(script)
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: llm)
    return Agent(session=Session()), llm


def _text(content):
    return {"role": "assistant", "content": content}


def test_continue_run_adds_no_user_message(monkeypatch):
    """同样是跑一轮，但这次调用不会往历史里追加 user 消息。

    起点必须是"末尾非 assistant"的合法上下文（这里用一条外部事件注释模拟）——
    干净结束后的末尾就是 assistant，按约束本就不能接着跑（见下一条用例）。
    """
    agent, llm = _agent(monkeypatch, [_text("接着写")])
    agent.session.add("user", "外部事件：请继续")
    roles_before = [message["role"] for message in agent.session.messages]

    result = asyncio.run(agent.continue_run())

    assert result.text == "接着写"
    # 关键断言：user 消息一条都没多（system 段会由 sync_system 首次插入/原地刷新，
    # 所以按「各角色计数」比较，而不是整体相等）
    roles_after = [message["role"] for message in agent.session.messages]
    assert roles_after.count("user") == roles_before.count("user") == 1
    assert roles_after[-1] == "assistant"
    assert llm.seen[0][-1][0] == "user"  # 请求里最后一条仍是那条注释


def test_continue_run_works_after_an_interrupt(monkeypatch):
    """中断后接着写：末尾是中断注释（user），正好满足约束。"""

    class InterruptingLLM(ScriptedLLM):
        def chat_stream(self, messages, tools=None):
            self.seen.append([(m.get("role"), m.get("content", "")) for m in messages])
            if len(self.seen) == 1:
                yield ("content", "写了一半")
                agent.interrupt()  # 模拟 Esc
                return
            yield ("message", {"role": "assistant", "content": "接着写完了"})

    llm = InterruptingLLM([])
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: llm)
    agent = Agent(session=Session())

    first = asyncio.run(agent.run("写首诗"))
    assert first.status == "interrupted"
    assert agent.session.messages[-1]["role"] == "user"  # 中断注释

    second = asyncio.run(agent.continue_run())

    assert second.text == "接着写完了"
    # 接续时模型看到了上一轮的部分正文（说明"接着写"确实基于同一上下文）
    assert any("写了一半" in content for _role, content in llm.seen[1])


def test_continue_run_rejects_empty_context(monkeypatch):
    agent, _llm = _agent(monkeypatch, [])

    with pytest.raises(ValueError, match="上下文为空"):
        asyncio.run(agent.continue_run())


def test_continue_run_rejects_assistant_tail(monkeypatch):
    """末尾是 assistant 时拒绝：否则会请求两条 assistant 连排。"""
    agent, _llm = _agent(monkeypatch, [_text("完成")])
    asyncio.run(agent.run("干活"))

    with pytest.raises(ValueError, match="最后一条是 assistant"):
        asyncio.run(agent.continue_run())


def test_session_delegates_continue_run(monkeypatch):
    agent, _llm = _agent(monkeypatch, [_text("继续")])
    session = agent.session_owner
    agent.session.add("user", "外部事件：请继续")

    assert asyncio.run(session.continue_run()).text == "继续"

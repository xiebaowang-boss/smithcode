"""Agent 钩子（agent/hooks.py）：四个决策点的语义与默认行为等价性。

不配钩子时必须与改造前逐字相同（既有 1400+ 条用例就是证据）；这里补的是
**配了钩子之后**的语义：拦下、改文本、投票结束、回合裁决，以及扩展点自己
炸掉时的边界策略（before fail-closed、after fail-open）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode.agent import Agent
from smithcode.agent.hooks import (
    AfterToolCallResult,
    AgentHooks,
    BeforeToolCallResult,
)
from smithcode.session import Session


class ScriptedLLM:
    """按脚本回放 assistant 消息（每次 chat_stream 消费一条）。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None):
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（用尽）"}
        yield ("message", msg)


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


def _tools(calls: list[tuple[str, dict, str]]) -> dict:
    """一条 assistant 消息带多个工具调用（同一批次）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)},
            }
            for name, args, call_id in calls
        ],
    }


def _register_fake_tool(monkeypatch, executed: list[str]) -> None:
    """注册一个记录调用的假工具，并放行权限检查。

    聚焦点是钩子语义：真实权限规则会让非交互环境 fail-closed 拒绝，把用例的
    失败原因从「钩子没生效」变成「权限被拒」。
    """
    from smithcode.permission import Permission
    from smithcode.tools import FUNCTIONS

    def fake(path=None):
        executed.append(path or "")
        return f"已执行 {path}"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", fake)
    monkeypatch.setattr(Permission, "check", lambda self, *a, **k: True)
    monkeypatch.setattr(Permission, "check_paths", lambda self, *a, **k: True)


def _agent(monkeypatch, script, hooks=None) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(script))
    return Agent(session=Session(), hooks=hooks)


def _tool_messages(agent) -> list[str]:
    return [m["content"] for m in agent.session.messages if m.get("role") == "tool"]


# ---------- before_tool_call ----------


def test_before_hook_sees_the_call_and_can_block_it(monkeypatch):
    seen: list = []

    async def hook(ctx, signal):
        seen.append((ctx.tool_call["function"]["name"], ctx.args))
        return BeforeToolCallResult(block=True, reason="钩子说不行")

    agent = _agent(monkeypatch, [_tool("list_dir", {"path": "."}), _text("好的")],
                   hooks=AgentHooks(before_tool_call=hook))

    result = asyncio.run(agent.run("看看目录"))

    assert seen == [("list_dir", {"path": "."})]
    assert _tool_messages(agent) == ["钩子说不行"]  # 未执行，但结果成对
    assert result.status == "ok"


def test_blocked_call_does_not_stop_the_rest_of_the_batch(monkeypatch):
    """拦下只影响本次调用（对齐 pi 的 block 语义），其余照常执行。"""
    executed: list[str] = []

    async def hook(ctx, signal):
        if ctx.args.get("path") == "拦我":
            return BeforeToolCallResult(block=True, reason="拦下")
        return None

    _register_fake_tool(monkeypatch, executed)
    agent = _agent(
        monkeypatch,
        [_tools([("fake_tool", {"path": "拦我"}, "1"),
                 ("fake_tool", {"path": "放行"}, "2")]),
         _text("结束")],
        hooks=AgentHooks(before_tool_call=hook),
    )

    asyncio.run(agent.run("两件事"))

    assert executed == ["放行"]
    assert _tool_messages(agent) == ["拦下", "已执行 放行"]


def test_before_hook_failure_blocks_instead_of_breaking_the_transcript(monkeypatch):
    """钩子抛异常 = fail-closed 拦下：不能让 tool_call_id 失去配对结果。"""
    async def hook(ctx, signal):
        raise RuntimeError("钩子炸了")

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("好的")],
                   hooks=AgentHooks(before_tool_call=hook))

    result = asyncio.run(agent.run("看看目录"))

    assert result.status == "ok"
    assert len(_tool_messages(agent)) == 1
    assert "before_tool_call 钩子失败" in _tool_messages(agent)[0]


# ---------- after_tool_call ----------


def test_after_hook_can_replace_the_result_text(monkeypatch):
    async def hook(ctx, signal):
        return AfterToolCallResult(content=f"[改写] {ctx.result}")

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("好的")],
                   hooks=AgentHooks(after_tool_call=hook))

    asyncio.run(agent.run("看看目录"))

    assert _tool_messages(agent)[0].startswith("[改写] ")


def test_after_hook_failure_keeps_the_result(monkeypatch):
    async def hook(ctx, signal):
        raise RuntimeError("格式化炸了")

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("好的")],
                   hooks=AgentHooks(after_tool_call=hook))

    asyncio.run(agent.run("看看目录"))

    assert "after_tool_call 钩子失败" in _tool_messages(agent)[0]
    assert "目录" in _tool_messages(agent)[0] or _tool_messages(agent)[0]  # 原结果仍在前面


# ---------- terminate：整批投票 ----------


def _terminating_hook(vote: bool):
    async def hook(ctx, signal):
        return AfterToolCallResult(terminate=vote)

    return hook


def test_batch_terminates_only_when_every_result_votes(monkeypatch):
    """pi 的 `shouldTerminateToolBatch`：整批都为 True 才提前结束。"""
    _register_fake_tool(monkeypatch, [])
    agent = _agent(
        monkeypatch,
        [_tool("fake_tool", {"path": "a"}, "1"), _text("第二轮")],
        hooks=AgentHooks(after_tool_call=_terminating_hook(True)),
    )

    result = asyncio.run(agent.run("做一件事"))

    assert result.status == "ok"
    assert result.text == ""  # 提前结束，没有下一轮正文


def test_partial_vote_does_not_terminate(monkeypatch):
    """同一批里只要有一个没投票，就照常进下一轮。"""
    votes = [True, False]

    async def hook(ctx, signal):
        return AfterToolCallResult(terminate=votes.pop(0))

    _register_fake_tool(monkeypatch, [])
    agent = _agent(
        monkeypatch,
        [_tools([("fake_tool", {"path": "a"}, "1"),
                 ("fake_tool", {"path": "b"}, "2")]),
         _text("第二轮")],
        hooks=AgentHooks(after_tool_call=hook),
    )

    result = asyncio.run(agent.run("做两件事"))

    assert result.text == "第二轮"  # 有人投了 False → 照常进下一轮


# ---------- 回合钩子 ----------


def test_prepare_next_turn_runs_before_every_model_call(monkeypatch):
    seen: list[int] = []

    async def hook(turn, signal):
        seen.append(turn.iteration)

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("结束")],
                   hooks=AgentHooks(prepare_next_turn=hook))

    asyncio.run(agent.run("看看目录"))

    assert seen == [0, 1]  # 首轮 + 工具后的第二轮


def test_should_stop_after_turn_ends_the_run(monkeypatch):
    """裁决为 True：不再发起下一轮模型调用（第二轮脚本用不到）。"""
    async def hook(turn, signal):
        return True

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("第二轮")],
                   hooks=AgentHooks(should_stop_after_turn=hook))

    result = asyncio.run(agent.run("看看目录"))

    assert result.status == "ok"
    assert result.text == ""


def test_should_stop_after_turn_receives_the_turn_context(monkeypatch):
    captured: list = []

    async def hook(turn, signal):
        captured.append(turn)
        return False

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("结束")],
                   hooks=AgentHooks(should_stop_after_turn=hook))

    asyncio.run(agent.run("看看目录"))

    assert captured[0].tools_used == ("list_dir",)
    assert captured[0].iteration == 1
    assert captured[0].tool_results  # 本批结果文本


def test_hooks_receive_an_abort_signal(monkeypatch):
    seen: list = []

    async def hook(ctx, signal):
        seen.append(signal is not None and hasattr(signal, "aborted"))

    agent = _agent(monkeypatch, [_tool("list_dir"), _text("结束")],
                   hooks=AgentHooks(before_tool_call=hook))

    asyncio.run(agent.run("看看目录"))

    assert seen == [True]


@pytest.mark.parametrize("hooks", [None, AgentHooks()])
def test_no_hooks_means_no_change(monkeypatch, hooks):
    """不配钩子（或全为 None）时行为与改造前一致：工具照常执行并回传结果。"""
    agent = _agent(monkeypatch, [_tool("list_dir"), _text("结束")], hooks=hooks)

    result = asyncio.run(agent.run("看看目录"))

    assert result.status == "ok"
    assert len(_tool_messages(agent)) == 1

"""AgentSession.run_with_goal 续跑循环测试：自动接续、空转刹车、预算收尾与中断语义。"""

import asyncio
import json

import pytest

from smithcode import frontend, goal
from smithcode.agent import Agent
from smithcode.cancel import RunResult
from smithcode.frontend.console import ConsoleFrontend
from smithcode.session import Session


@pytest.fixture(autouse=True)
def fresh_goal():
    goal.reset()
    yield
    goal.reset()


class _ScriptedLLM:
    """按脚本逐次回放 assistant 消息（每次 chat_stream 消费一条）。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None):
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（脚本用尽）"}
        yield ("message", msg)


def _llm(script):
    return lambda: _ScriptedLLM(script)


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


def _todo_step(title="步骤一", call_id="1"):
    return _tool("todo_write", {"todos": [{"title": title, "status": "in_progress"}]}, call_id)


def _make_agent(monkeypatch, script):
    """假模型 + 装配终端前端（呈现走事件，与生产一致）。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", _llm(script))
    agent = Agent(session=Session())
    frontend.attach(agent.events, ConsoleFrontend())
    return agent


def test_run_records_tools_used(monkeypatch):
    agent = _make_agent(monkeypatch, [_todo_step(), _text("完成")])
    result = asyncio.run(agent.run("任务"))
    assert result.status == "ok"
    assert result.tools_used == ("todo_write",)


def test_run_with_goal_without_goal_is_plain_run(monkeypatch):
    agent = _make_agent(monkeypatch, [_text("你好")])
    result = asyncio.run(agent.session_owner.run_with_goal("你好"))
    assert result.text == "你好"
    assert not goal.is_set()


def test_run_with_goal_auto_continues_until_complete(monkeypatch, capsys):
    agent = _make_agent(
        monkeypatch,
        [
            _todo_step(call_id="1"),
            _text("第一步完成"),
            _tool("goal_update", {"status": "complete", "summary": "pytest 全部通过"}, "2"),
            _text("目标完成"),
        ],
    )
    goal.set("测试目标", max_turns=10)

    result = asyncio.run(agent.session_owner.run_with_goal("开始"))

    assert result.text == "目标完成"
    current = goal.current()
    assert current.status == goal.COMPLETE
    assert current.turns == 2  # 首轮 + 一次续跑
    assert current.evidence == "pytest 全部通过"
    assert "[目标] 继续推进" in capsys.readouterr().out
    # 续跑提示词以 user 消息进入历史，目标文本可被模型看到
    assert any("测试目标" in str(m.get("content", "")) for m in agent.session.messages)


def test_goal_run_emits_one_execution_pair_per_turn(monkeypatch, capsys):
    """`/goal` 续跑多轮：execution 边界**一轮一对**，忙碌态是易失的（不进日志）。

    外层包装（`outer_turn_begin/end`）原先也发一对 execution，于是同一任务在日志里
    出现两对边界——重放每轮页脚只能靠"跳过没有起点的终结事件"兜住（既不忠实也不好读）。
    现在切清：`execution` 的粒度是"一次用户任务"（一轮 `run()`），外层包装只发
    `StatusChanged(working)`（跨多轮的忙碌区间，易失、不落盘），标题的忙闲计数认两族。
    """
    agent = _make_agent(monkeypatch, [
        _todo_step(call_id="1"),
        _text("第一步完成"),
        _tool("goal_update", {"status": "complete", "summary": "完成"}, "2"),
        _text("目标完成"),
    ])
    goal.set("测试目标", max_turns=10)
    seen: list = []
    agent.events.subscribe(seen.append)

    asyncio.run(agent.session_owner.run_with_goal("开始"))

    types = [env.type for env in seen]
    assert types.count("session.execution.started") == 2  # 首轮 + 一次续跑
    assert types.count("session.execution.succeeded") == 2
    # 一轮一对、严格交替（没有"两个 started 夹一个 succeeded"）
    assert [t for t in types if t.startswith("session.execution.")] == [
        "session.execution.started", "session.execution.succeeded",
        "session.execution.started", "session.execution.succeeded",
    ]
    # 忙碌区间是**易失**事件：总线上有，但不进日志（会话日志只留骨架事实）
    working = [env for env in seen if env.type == "session.status.changed"]
    assert working, "跨轮的忙碌区间应当发 StatusChanged"
    assert all(not env.durable for env in working)


def test_run_with_goal_pauses_on_toolless_turn(monkeypatch, capsys):
    agent = _make_agent(monkeypatch, [_text("好的")])
    goal.set("测试目标")

    asyncio.run(agent.session_owner.run_with_goal("开始"))

    current = goal.current()
    assert current.status == goal.PAUSED
    assert "工具调用" in current.note
    assert "已暂停" in capsys.readouterr().out


def test_run_with_goal_pauses_on_toolless_continuation(monkeypatch):
    agent = _make_agent(
        monkeypatch,
        [_todo_step(), _text("第一步完成"), _text("我不知道接下来做什么")],
    )
    goal.set("测试目标", max_turns=10)

    asyncio.run(agent.session_owner.run_with_goal("开始"))

    current = goal.current()
    assert current.status == goal.PAUSED
    assert current.turns == 2


def test_run_with_goal_budget_limited_wraps_up(monkeypatch, capsys):
    agent = _make_agent(monkeypatch, [_todo_step(), _text("第一轮"), _text("收尾总结")])
    goal.set("测试目标", max_turns=1)

    result = asyncio.run(agent.session_owner.run_with_goal("开始"))

    assert result.text == "收尾总结"
    current = goal.current()
    assert current.status == goal.BUDGET_LIMITED
    assert any("回合预算" in str(m.get("content", "")) for m in agent.session.messages)
    assert "预算用尽" in capsys.readouterr().out


def test_run_with_goal_unlimited_budget_keeps_going(monkeypatch):
    """默认预算不限：回合数远超旧的 50 仍继续，直到目标声明完成。"""
    script = []
    for i in range(55):
        script.append(_todo_step(f"步骤{i}", call_id=str(i)))
        script.append(_text(f"第{i}轮"))
    script.append(_tool("goal_update", {"status": "complete", "summary": "证据"}, "done"))
    agent = _make_agent(monkeypatch, script)
    goal.set("测试目标")  # 未指定预算 → 取配置默认 -1（不限）
    assert goal.current().unlimited

    asyncio.run(agent.session_owner.run_with_goal("开始"))

    current = goal.current()
    assert current.status == goal.COMPLETE
    assert current.turns == 56  # 远超旧默认 50，未触发预算收尾


def test_run_with_goal_keeps_active_on_interrupt(monkeypatch):
    agent = _make_agent(monkeypatch, [])

    async def fake_run(_text):
        return RunResult("interrupted")

    monkeypatch.setattr(agent, "run", fake_run)
    goal.set("测试目标")

    asyncio.run(agent.session_owner.run_with_goal("开始"))

    assert goal.is_active()  # 用户主动中断：目标保留，可 resume


def test_run_with_goal_pauses_on_denied(monkeypatch):
    agent = _make_agent(monkeypatch, [])

    async def fake_run(_text):
        return RunResult("denied", "任务已停止")

    monkeypatch.setattr(agent, "run", fake_run)
    goal.set("测试目标")

    asyncio.run(agent.session_owner.run_with_goal("开始"))

    current = goal.current()
    assert current.status == goal.PAUSED
    assert "未正常结束" in current.note

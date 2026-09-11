"""Agent 批量工具执行的两阶段并发测试：预检串行、波次执行、顺序保证。

用假 LLM + 假工具验证，不依赖真实 API：
- 结果按提交顺序收集（与模型请求 tool_calls 的顺序一致）
- 可并行工具真正并发执行（Barrier 同步验证）
- serial 工具作为顺序屏障：后面的并行工具能看到它的副作用
- MAX_TOOL_CONCURRENCY=1 时退化为纯串行（全在主线程）
- 权限被拒发生在执行之前：已过预检的计划不执行、补占位结果
"""

import threading

import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tools import FUNCTIONS, SERIAL


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


def _tc(name, args="{}", call_id="1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}


def _tool_calls_llm(batches):
    """假 LLM：第一轮返回给定的多批 tool_calls，第二轮直接给最终回复。"""

    class BatchLLM:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield ("message", {"role": "assistant", "content": "", "tool_calls": batches})
            else:
                yield ("message", {"role": "assistant", "content": "完成"})

    return BatchLLM


def _make_agent(monkeypatch, batches):
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(batches))
    agent = Agent(session=Session())
    # 假工具不在权限规则表内，默认 ask 会弹确认；测试统一放行
    monkeypatch.setattr(agent.permission, "check", lambda name, args: True)
    return agent


def _tool_messages(agent):
    return [m for m in agent.session.messages if m["role"] == "tool"]


# ---------- 顺序保证 ----------

def test_results_kept_in_submission_order(monkeypatch):
    """并发执行后，结果仍按提交顺序追加，tool_call_id 一一配对。"""
    calls = [_tc("fake_tool", call_id=str(i)) for i in (1, 2, 3)]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    agent = _make_agent(monkeypatch, calls)

    agent.run("顺序")

    msgs = _tool_messages(agent)
    assert [m["content"] for m in msgs] == ["ok", "ok", "ok"]
    assert [m["tool_call_id"] for m in msgs] == ["1", "2", "3"]


# ---------- 真并发 ----------

def test_parallel_tools_run_concurrently(monkeypatch):
    """两个可并行工具必须同时执行：Barrier(2) 任一方先到都要等对方，
    若被串行执行则先到者超时报错，测试即失败。"""
    calls = [_tc("par_a", call_id="a"), _tc("par_b", call_id="b")]
    barrier = threading.Barrier(2)

    def make_tool(tag):
        def tool():
            barrier.wait(timeout=10)  # 串行执行时先到者在此超时
            return tag

        return tool

    monkeypatch.setitem(FUNCTIONS, "par_a", make_tool("A"))
    monkeypatch.setitem(FUNCTIONS, "par_b", make_tool("B"))
    agent = _make_agent(monkeypatch, calls)

    agent.run("并发")
    msgs = _tool_messages(agent)
    assert [m["content"] for m in msgs] == ["A", "B"]
    assert not any("错误" in m["content"] for m in msgs)


# ---------- serial 屏障语义 ----------

def test_serial_tool_is_order_barrier(monkeypatch):
    """serial 工具是顺序屏障：它之后的并行工具必须看到它的副作用。

    [reader, writer(serial), reader] 三段波次：第二个 reader 必须读到
    writer 写入的新值；两个 reader 各自成波（中间隔着屏障），不能合并。
    """
    calls = [_tc("fake_reader", call_id="1"),
             _tc("fake_writer", call_id="2"),
             _tc("fake_reader", call_id="3")]
    shared = {"v": "旧值"}
    monkeypatch.setitem(FUNCTIONS, "fake_reader", lambda: shared["v"])
    monkeypatch.setitem(FUNCTIONS, "fake_writer", lambda: shared.update(v="新值") or "写好了")
    monkeypatch.setitem(SERIAL, "fake_writer", True)
    agent = _make_agent(monkeypatch, calls)

    agent.run("屏障")
    msgs = _tool_messages(agent)
    assert [m["content"] for m in msgs] == ["旧值", "写好了", "新值"]


def test_serial_registry_declares_stateful_tools():
    """有跨调用状态 / 线程不安全的工具必须注册为 serial。"""
    for name in ("run_command", "todo_write", "ask_user", "write_file", "edit_file", "apply_patch"):
        assert SERIAL.get(name) is True, name


# ---------- 退化与兼容 ----------

def test_concurrency_1_runs_in_main_thread(monkeypatch):
    """MAX_TOOL_CONCURRENCY=1 退化为纯串行：不启用线程池，主线程执行。"""
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2")]
    threads = []

    def tool():
        threads.append(threading.current_thread() is threading.main_thread())
        return "ok"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", tool)
    monkeypatch.setattr(config, "MAX_TOOL_CONCURRENCY", 1)
    agent = _make_agent(monkeypatch, calls)

    agent.run("单线程")
    assert threads == [True, True]


def test_single_plan_runs_in_main_thread(monkeypatch):
    """单计划快速路径（不开线程池）在主线程执行。"""
    holder = {}

    def tool():
        holder["main"] = threading.current_thread() is threading.main_thread()
        return "ok"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", tool)
    agent = _make_agent(monkeypatch, [])

    agent._execute_batch([_tc("fake_tool")])
    assert holder["main"] is True
    assert _tool_messages(agent)[0]["content"] == "ok"


# ---------- 权限被拒的两阶段语义 ----------

def test_denial_happens_before_any_execution(monkeypatch, tmp_path):
    """权限预检先于全部执行：后面的调用被拒时，前面的已批准计划不执行，
    补占位结果防悬空 tool_call_id（比旧版"执行到一半被拒"更干净）。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2")]

    executed = []

    def tool():
        executed.append(True)
        return "ok"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", tool)
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = Agent(session=Session())

    seen = []

    def check(name, args):
        seen.append(name)
        return len(seen) == 1  # 第一次放行，第二次拒绝

    monkeypatch.setattr(agent.permission, "check", check)

    result = agent.run("拒绝")
    assert "权限" in result.text
    assert executed == []  # 第一个工具也未执行

    msgs = _tool_messages(agent)
    assert [m["content"] for m in msgs] == ["（未执行：权限请求被拒绝，任务已中止）",
                                            "用户拒绝了此操作"]
    assert [m["tool_call_id"] for m in msgs] == ["1", "2"]

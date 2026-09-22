"""Agent 批量工具执行的两阶段并发测试：预检串行、波次执行、顺序保证。

用假 LLM + 假工具验证，不依赖真实 API：
- 结果按提交顺序收集（与模型请求 tool_calls 的顺序一致）
- 可并行工具真正并发执行（Barrier 同步验证）
- serial 工具作为顺序屏障：后面的并行工具能看到它的副作用
- MAX_TOOL_CONCURRENCY=1 时退化为纯串行（工具不重叠执行）
- 权限被拒发生在执行之前：已过预检的计划不执行、补占位结果
"""

import asyncio
import threading
import time

import pytest

from smithcode import config, frontend
from smithcode.agent import Agent
from smithcode.frontend.console import ConsoleFrontend
from smithcode.session import Session
from smithcode.tools import FUNCTIONS, SERIAL


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)




def _console_agent(**kwargs):
    """建 Agent 并装配终端前端：呈现走事件（与生产一致），用例用 capsys 读输出。"""
    agent = Agent(session=Session(), **kwargs)
    frontend.attach(agent.events, ConsoleFrontend())
    # 直接调内部入口（绕过 run）时也要有活动端口：提问是经它 await 前端的
    from smithcode.event import asks as _ask_port

    _ask_port.activate(agent.asks)
    return agent


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
    agent = _console_agent()
    # 假工具不在权限规则表内，默认 ask 会弹确认；测试统一放行
    async def _fake_check(name, args, content=None):
        return True
    monkeypatch.setattr(agent.permission, "check", _fake_check)
    return agent


def _tool_messages(agent):
    return [m for m in agent.session.messages if m["role"] == "tool"]


# ---------- 顺序保证 ----------

def test_results_kept_in_submission_order(monkeypatch):
    """并发执行后，结果仍按提交顺序追加，tool_call_id 一一配对。"""
    calls = [_tc("fake_tool", call_id=str(i)) for i in (1, 2, 3)]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    agent = _make_agent(monkeypatch, calls)

    asyncio.run(agent.run("顺序"))

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

    asyncio.run(agent.run("并发"))
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

    asyncio.run(agent.run("屏障"))
    msgs = _tool_messages(agent)
    assert [m["content"] for m in msgs] == ["旧值", "写好了", "新值"]


def test_serial_registry_declares_stateful_tools():
    """有跨调用状态 / 线程不安全的工具必须注册为 serial。"""
    for name in ("run_command", "todo_write", "ask_user", "write_file", "edit_file", "apply_patch"):
        assert SERIAL.get(name) is True, name


# ---------- 退化与兼容 ----------

def test_concurrency_1_serializes_tool_execution(monkeypatch):
    """MAX_TOOL_CONCURRENCY=1 退化为纯串行：两个工具不重叠执行。

    异步化之前这里断言的是「在主线程序」——那是线程池的实现细节；改断言真正
    的不变量：同一时刻只有一个工具在执行（I7 的意图）。
    """
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2")]
    active = 0
    overlapped: list[bool] = []

    def tool():
        nonlocal active
        active += 1
        overlapped.append(active > 1)
        time.sleep(0.03)
        active -= 1
        return "ok"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", tool)
    monkeypatch.setattr(config, "MAX_TOOL_CONCURRENCY", 1)
    agent = _make_agent(monkeypatch, calls)

    asyncio.run(agent.run("单线程"))
    assert overlapped == [False, False]
    assert [m["content"] for m in _tool_messages(agent)] == ["ok", "ok"]


def test_single_plan_executes_without_overlap(monkeypatch):
    """单计划快速路径（不并发）：照常执行并按序回传结果。"""
    holder = {}

    def tool():
        holder["executed"] = True
        return "ok"

    monkeypatch.setitem(FUNCTIONS, "fake_tool", tool)
    agent = _make_agent(monkeypatch, [])

    asyncio.run(agent._execute_batch([_tc("fake_tool")]))
    assert holder["executed"] is True
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
    agent = _console_agent()

    seen = []

    async def check(name, args, content=None):
        seen.append(name)
        return len(seen) == 1  # 第一次放行，第二次拒绝

    monkeypatch.setattr(agent.permission, "check", check)

    result = asyncio.run(agent.run("拒绝"))
    assert "权限" in result.text
    assert executed == []  # 第一个工具也未执行

    msgs = _tool_messages(agent)
    assert [m["content"] for m in msgs] == ["（未执行：权限请求被拒绝，任务已中止）",
                                            "用户拒绝了此操作"]
    assert [m["tool_call_id"] for m in msgs] == ["1", "2"]


# ---------- 流式调度：边预检边执行 ----------

def test_serial_executes_before_later_preflight(monkeypatch, tmp_path):
    """流式调度：串行工具在"后续工具的权限确认"之前就已执行。

    旧两阶段会先把 cmd2 也确认了才执行 cmd1；流式下 cmd1 先跑完再确认 cmd2。
    """
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    events = []

    def par_tool(tag=""):
        events.append(("run", tag))
        return "par"

    def cmd_tool(tag=""):
        events.append(("run", tag))
        return "cmd"

    monkeypatch.setitem(FUNCTIONS, "par_tool", par_tool)
    monkeypatch.setitem(FUNCTIONS, "cmd_tool", cmd_tool)
    monkeypatch.setitem(SERIAL, "cmd_tool", True)

    calls = [
        _tc("par_tool", args='{"tag": "read"}', call_id="1"),
        _tc("cmd_tool", args='{"tag": "cmd1"}', call_id="2"),
        _tc("cmd_tool", args='{"tag": "cmd2"}', call_id="3"),
    ]
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = _console_agent()

    async def check(name, args, content=None):
        events.append(("check", args.get("tag")))
        return True

    monkeypatch.setattr(agent.permission, "check", check)
    asyncio.run(agent.run("流式"))

    assert events.index(("run", "cmd1")) < events.index(("check", "cmd2"))


def test_wave_runs_before_serial_barrier(monkeypatch, tmp_path):
    """屏障前的并行波次真并发执行，且都先于串行工具执行。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    order = []
    barrier = threading.Barrier(2)

    def make_par(tag):
        def tool():
            barrier.wait(timeout=10)  # 串行执行时先到者超时 → 测试失败
            order.append(("run", tag))
            return tag

        return tool

    def cmd_tool(tag=""):
        order.append(("run", tag))
        return "cmd"

    monkeypatch.setitem(FUNCTIONS, "par_a", make_par("a"))
    monkeypatch.setitem(FUNCTIONS, "par_b", make_par("b"))
    monkeypatch.setitem(FUNCTIONS, "cmd_tool", cmd_tool)
    monkeypatch.setitem(SERIAL, "cmd_tool", True)

    calls = [
        _tc("par_a", call_id="1"),
        _tc("par_b", call_id="2"),
        _tc("cmd_tool", args='{"tag": "c"}', call_id="3"),
    ]
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = _console_agent()
    async def _fake_check(name, args, content=None):
        return True
    monkeypatch.setattr(agent.permission, "check", _fake_check)

    asyncio.run(agent.run("屏障"))

    assert order.index(("run", "a")) < order.index(("run", "c"))
    assert order.index(("run", "b")) < order.index(("run", "c"))


def test_denied_after_executed_serial_keeps_partial(monkeypatch, tmp_path):
    """流式的 partial-apply 语义：已执行的串行工具保留，之后的被拒只占位其后。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    executed = []

    def cmd_tool():
        executed.append("cmd")
        return "cmd done"

    monkeypatch.setitem(FUNCTIONS, "cmd_tool", cmd_tool)
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "no")
    monkeypatch.setitem(SERIAL, "cmd_tool", True)

    calls = [_tc("cmd_tool", call_id="1"), _tc("fake_tool", call_id="2")]
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = _console_agent()

    seen = []

    async def _fake_check(name, args, content=None):
        seen.append(name)
        return name == "cmd_tool"  # 只放行串行工具

    monkeypatch.setattr(agent.permission, "check", _fake_check)

    result = asyncio.run(agent.run("拒绝"))
    assert "权限" in result.text
    assert executed == ["cmd"]  # 串行工具已执行、保留（不回滚）

    msgs = _tool_messages(agent)
    assert msgs[0]["content"] == "cmd done"
    assert msgs[1]["content"] == "用户拒绝了此操作"


# ---------- 展示分组的隐式契约 ----------

def test_tool_start_events_precede_ordered_results(monkeypatch):
    """TUI 的「已探索」分组依赖：同批 tool_calls 的 ToolStart 全部先于结果到达，
    且结果按请求顺序到达（不按完成顺序）。id 由模型给出（agent 侧生成）。"""
    calls = [_tc("fake_tool", call_id=str(i)) for i in (1, 2, 3)]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    agent = _make_agent(monkeypatch, calls)

    class CapSubscriber:
        """记录工具事件的到达顺序（前端就是这么消费的）。"""

        def __init__(self):
            self.events = []

        def __call__(self, env):
            from smithcode.event.catalog import ToolEnd, ToolStart

            if isinstance(env.data, ToolStart):
                self.events.append(("start", env.data.tool_call_id))
            elif isinstance(env.data, ToolEnd):
                self.events.append(("result", env.data.tool_call_id))

    cap = CapSubscriber()
    agent.events.subscribe(cap)
    asyncio.run(agent.run("契约"))

    kinds = [kind for kind, _ in cap.events]
    last_start = max(i for i, kind in enumerate(kinds) if kind == "start")
    first_result = min(i for i, kind in enumerate(kinds) if kind == "result")
    assert last_start < first_result  # 全部 start 先于任何 result
    assert [arg for kind, arg in cap.events if kind == "start"] == ["1", "2", "3"]
    assert [arg for kind, arg in cap.events if kind == "result"] == ["1", "2", "3"]


class _CapSubscriber:
    """记录 (tool_call_id, result) 的订阅者，验证 pending 工具块被收尾。"""

    def __init__(self):
        self.results = []

    def __call__(self, env):
        from smithcode.event.catalog import ToolEnd

        if isinstance(env.data, ToolEnd):
            self.results.append((env.data.tool_call_id, env.data.result))


def test_skipped_plan_closes_pending_widget_on_denial(monkeypatch, tmp_path):
    """权限被拒：此前已预检未执行的计划也要补 tool_result，否则 TUI 工具块停在 pending。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2")]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = _console_agent()

    seen = []

    async def _fake_check(name, args, content=None):
        seen.append(name)
        return len(seen) == 1  # 第一次放行，第二次拒绝

    monkeypatch.setattr(agent.permission, "check", _fake_check)
    cap = _CapSubscriber()
    agent.events.subscribe(cap)

    asyncio.run(agent.run("拒绝"))

    # 计划 1 被跳过 → SKIPPED；计划 2 被拒 → DENIED（id 是模型的 tool_call_id）
    assert cap.results[0] == ("1", "（未执行：权限请求被拒绝，任务已中止）")
    assert cap.results[1][0] == "2" and cap.results[1][1] == "用户拒绝了此操作"


def test_skipped_plan_closes_pending_widget_on_interrupt(monkeypatch):
    """中断：已预检未执行的计划补 tool_result 收尾 pending 工具块。

    流式调度下「已预检未执行」即缓冲区里的波次计划（此例为前两个并行计划）。
    """
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2"),
             _tc("fake_tool", call_id="3")]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = _console_agent()

    seen = []

    async def check(name, args, content=None):
        seen.append(name)
        if len(seen) == 3:  # 预检第 3 个时用户按 Esc
            agent.interrupt()
        return True

    monkeypatch.setattr(agent.permission, "check", check)
    cap = _CapSubscriber()
    agent.events.subscribe(cap)

    asyncio.run(agent.run("中断"))

    # 三个计划都已预检（各建了 pending 块）、都未执行 → 一律 INTERRUPTED 收尾
    assert [r[0] for r in cap.results] == ["1", "2", "3"]
    assert all(r[1] == "（未执行：用户中断了任务）" for r in cap.results)
    assert len(_tool_messages(agent)) == 3  # 会话完整性：每个 id 都有配对结果

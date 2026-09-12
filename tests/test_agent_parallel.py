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
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: True)
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

    def check(name, args, content=None):
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
    agent = Agent(session=Session())

    def check(name, args, content=None):
        events.append(("check", args.get("tag")))
        return True

    monkeypatch.setattr(agent.permission, "check", check)
    agent.run("流式")

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
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: True)

    agent.run("屏障")

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
    agent = Agent(session=Session())

    seen = []
    monkeypatch.setattr(
        agent.permission, "check",
        lambda name, args, content=None: seen.append(name) or name == "cmd_tool",
    )

    result = agent.run("拒绝")
    assert "权限" in result.text
    assert executed == ["cmd"]  # 串行工具已执行、保留（不回滚）

    msgs = _tool_messages(agent)
    assert msgs[0]["content"] == "cmd done"
    assert msgs[1]["content"] == "用户拒绝了此操作"


# ---------- 展示分组的隐式契约 ----------

def test_tool_start_events_precede_ordered_results(monkeypatch):
    """TUI 的「已探索」分组依赖：同批 tool_calls 的 tool_call（start）全部
    先于结果到达，且结果按请求顺序到达（不按完成顺序）。"""
    calls = [_tc("fake_tool", call_id=str(i)) for i in (1, 2, 3)]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    agent = _make_agent(monkeypatch, calls)

    events = []

    class CapRenderer:
        def __init__(self):
            self.seq = 0

        def tool_call(self, line, display="inline", name=""):
            self.seq += 1
            events.append(("start", self.seq))
            return self.seq

        def tool_result(self, result, tool_id=None, expand=False):
            events.append(("result", tool_id))

        def stream(self, kind, chunk):
            pass

        def stream_done(self):
            pass

        def info(self, text):
            pass

    monkeypatch.setattr("smithcode.renderer._current", CapRenderer())
    agent.run("契约")

    kinds = [kind for kind, _ in events]
    last_start = max(i for i, kind in enumerate(kinds) if kind == "start")
    first_result = min(i for i, kind in enumerate(kinds) if kind == "result")
    assert last_start < first_result  # 全部 start 先于任何 result
    assert [arg for kind, arg in events if kind == "start"] == [1, 2, 3]
    assert [arg for kind, arg in events if kind == "result"] == [1, 2, 3]  # 按请求顺序


class _CapRenderer:
    """记录 (tool_id, result) 的假渲染后端，验证 pending 工具块被收尾。"""

    def __init__(self):
        self.seq = 0
        self.results = []

    def tool_call(self, line, display="inline", name=""):
        self.seq += 1
        return self.seq

    def tool_result(self, result, tool_id=None, expand=False):
        self.results.append((tool_id, result))

    def stream(self, kind, chunk):
        pass

    def stream_done(self):
        pass

    def info(self, text):
        pass


def test_skipped_plan_closes_pending_widget_on_denial(monkeypatch, tmp_path):
    """权限被拒：此前已预检未执行的计划也要补 tool_result，否则 TUI 工具块停在 pending。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2")]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = Agent(session=Session())

    seen = []
    monkeypatch.setattr(
        agent.permission, "check",
        lambda name, args, content=None: seen.append(name) or len(seen) == 1,  # 第二次拒绝
    )
    cap = _CapRenderer()
    monkeypatch.setattr("smithcode.renderer._current", cap)

    agent.run("拒绝")

    # 计划 1（tool_id=1）被跳过 → SKIPPED；计划 2（tool_id=2）被拒 → DENIED
    assert cap.results[0] == (1, "（未执行：权限请求被拒绝，任务已中止）")
    assert cap.results[1][0] == 2 and cap.results[1][1] == "用户拒绝了此操作"


def test_skipped_plan_closes_pending_widget_on_interrupt(monkeypatch):
    """中断：已预检未执行的计划补 tool_result 收尾 pending 工具块。

    流式调度下「已预检未执行」即缓冲区里的波次计划（此例为前两个并行计划）。
    """
    calls = [_tc("fake_tool", call_id="1"), _tc("fake_tool", call_id="2"),
             _tc("fake_tool", call_id="3")]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda: "ok")
    monkeypatch.setattr("smithcode.agent.LLMClient", _tool_calls_llm(calls))
    agent = Agent(session=Session())

    seen = []

    def check(name, args, content=None):
        seen.append(name)
        if len(seen) == 3:  # 预检第 3 个时用户按 Esc
            agent.interrupt()
        return True

    monkeypatch.setattr(agent.permission, "check", check)
    cap = _CapRenderer()
    monkeypatch.setattr("smithcode.renderer._current", cap)

    agent.run("中断")

    # 三个计划都已预检（各建了 pending 块）、都未执行 → 一律 INTERRUPTED 收尾
    assert [r[0] for r in cap.results] == [1, 2, 3]
    assert all(r[1] == "（未执行：用户中断了任务）" for r in cap.results)
    assert len(_tool_messages(agent)) == 3  # 会话完整性：每个 id 都有配对结果

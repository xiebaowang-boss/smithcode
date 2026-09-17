"""Agent 主循环测试：用假 LLM 验证流式消费、循环与终止逻辑，不依赖真实 API。"""

import json
from types import SimpleNamespace

import httpx2
import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.context import truncate_output
from smithcode.session import Session
from smithcode.tools import FUNCTIONS


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


def _fake_tool_call(name="list_dir", args="{}"):
    """与 chat_stream 组装出的消息结构一致的 tool_calls 项。"""
    return {
        "id": "1",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class FakeLLM:
    """单轮即返回最终回复的假 LLM（流式接口）。"""

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        yield ("message", {"role": "assistant", "content": "最终回复"})


def _make_agent(monkeypatch) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def test_run_returns_final_content(monkeypatch):
    agent = _make_agent(monkeypatch)
    result = agent.run("打个招呼")
    assert result.status == "ok"
    assert result.text == "最终回复"


def test_run_records_messages(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent.run("打个招呼")
    roles = [m["role"] for m in agent.session.messages]
    assert roles == ["system", "user", "assistant"]


def test_agent_loop_caps_and_wraps_up(monkeypatch):
    class ToolCallLoopLLM(FakeLLM):
        """持续请求工具；收尾轮（tools=None）返回纯文本总结。"""

        def __init__(self):
            super().__init__()
            self.tool_turns = 0
            self.tools_seen = []

        def chat_stream(self, messages, tools=None):
            self.calls += 1
            self.tools_seen.append(tools)
            if tools is None:  # 收尾轮：不暴露工具，直接给总结
                yield ("content", "总结：已完成 X，剩余 Y")
                yield ("message", {"role": "assistant", "content": "总结：已完成 X，剩余 Y"})
                return
            self.tool_turns += 1
            yield ("message", {"role": "assistant", "content": "", "tool_calls": [_fake_tool_call()]})

    monkeypatch.setattr("smithcode.agent.LLMClient", ToolCallLoopLLM)
    agent = Agent(session=Session(), max_iterations=2)
    result = agent.run("死循环")
    assert result.status == "max_iterations"
    assert result.text == "总结：已完成 X，剩余 Y"
    assert agent.llm.tool_turns == 2  # 只执行配置的 2 轮工具
    assert agent.llm.tools_seen[-1] is None  # 收尾轮不暴露任何工具


def test_agent_loop_unlimited_by_default(monkeypatch):
    """未配置 max_iterations（默认 -1）时不封顶：可远超旧的 30 轮。"""

    class LongLoopLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls <= 40:
                yield (
                    "message",
                    {"role": "assistant", "content": "", "tool_calls": [_fake_tool_call()]},
                )
            else:
                yield ("message", {"role": "assistant", "content": "终于完成"})

    monkeypatch.setattr("smithcode.agent.LLMClient", LongLoopLLM)
    agent = Agent(session=Session())
    assert agent.max_iterations == -1
    result = agent.run("多轮任务")
    assert result.status == "ok"
    assert result.text == "终于完成"


def test_wrap_up_strips_unexpected_tool_calls(monkeypatch):
    """收尾轮模型仍返回 tool_calls 时一律剥离，历史里不留悬空 tool_call_id。"""

    class StubbornLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            self.calls += 1
            yield (
                "message",
                {"role": "assistant", "content": "部分总结", "tool_calls": [_fake_tool_call()]},
            )

    monkeypatch.setattr("smithcode.agent.LLMClient", StubbornLLM)
    agent = Agent(session=Session(), max_iterations=1)
    result = agent.run("顽固")
    assert result.status == "max_iterations"
    assert result.text == "部分总结"
    last = agent.session.messages[-1]
    assert last["role"] == "assistant" and "tool_calls" not in last


def test_run_stops_when_permission_denied(monkeypatch, tmp_path):
    """权限被拒：任务立即终止，同批剩余 tool_calls 补占位结果（防悬空 tool_call_id）。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])

    class TwoToolCallsLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        _fake_tool_call("read_file", json.dumps({"path": "a.txt"})),
                        _fake_tool_call("list_dir", "{}"),
                    ],
                },
            )

    monkeypatch.setattr("smithcode.agent.LLMClient", TwoToolCallsLLM)
    agent = Agent(session=Session())
    agent.permission.user_rules = [("read_file", "*", "deny")]

    result = agent.run("测试拒绝流程")
    assert result.status == "denied"
    assert "权限" in result.text

    tool_msgs = [m for m in agent.session.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert "拒绝" in tool_msgs[0]["content"]
    assert "未执行" in tool_msgs[1]["content"]


def test_truncate_output_short_text_unchanged():
    assert truncate_output("短输出", 100) == "短输出"


def test_truncate_output_keeps_head_and_tail():
    text = "A" * 800 + "中间被丢弃的部分" + "B" * 800
    result = truncate_output(text, 200)
    assert result.startswith("A" * 100)
    assert result.endswith("B" * 100)
    assert "已省略" in result
    assert "中间被丢弃的部分" not in result


def test_run_truncates_oversized_tool_result(monkeypatch):
    """超长工具结果在写入会话前应被截断，避免撑爆上下文。"""

    class OneToolThenDoneLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            self.calls += 1
            if self.calls == 1:
                yield (
                    "message",
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [_fake_tool_call("big_tool", "{}")],
                    },
                )
            else:
                yield ("message", {"role": "assistant", "content": "完成"})

    monkeypatch.setattr("smithcode.agent.LLMClient", OneToolThenDoneLLM)
    monkeypatch.setitem(FUNCTIONS, "big_tool", lambda: "x" * 5000)
    # 把上限调小，避免测试里塞几万字符
    monkeypatch.setattr("smithcode.config.MAX_TOOL_OUTPUT", 1000)
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: True)

    agent.run("大输出")

    # 消息顺序: system, user, assistant(工具调用), tool(结果), assistant(最终回复)
    tool_msg = agent.session.messages[-2]
    assert len(tool_msg["content"]) < 5000
    assert "已省略" in tool_msg["content"]


def test_reasoning_shown_but_not_persisted(monkeypatch, capsys):
    """思考内容应实时展示，但不写入会话（多数兼容服务不接受回传）。"""

    class ReasoningLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield ("reasoning", "先想想")
            yield ("content", "你好")
            yield ("message", {"role": "assistant", "content": "你好"})

    monkeypatch.setattr("smithcode.agent.LLMClient", ReasoningLLM)
    agent = Agent(session=Session())

    assert agent.run("打个招呼").text == "你好"

    out = capsys.readouterr().out
    assert "先想想" in out
    for m in agent.session.messages:
        assert "先想想" not in str(m.get("content", ""))


def test_reasoning_and_content_on_separate_lines(monkeypatch, capsys):
    """思考段与正文段各占一行，正文行也带 助手> 前缀。"""

    class ThinkThenAnswerLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield ("reasoning", "想一想")
            yield ("content", "答案")
            yield ("message", {"role": "assistant", "content": "答案"})

    monkeypatch.setattr("smithcode.agent.LLMClient", ThinkThenAnswerLLM)
    agent = Agent(session=Session())

    msg, usage, interrupted = agent._chat()
    assert msg == {"role": "assistant", "content": "答案"}
    assert usage is None  # 假 LLM 没发 usage 事件
    assert interrupted is False

    out = capsys.readouterr().out
    assert "[Thinking] 想一想" in out
    assert "助手> 答案" in out


# ---------- /new 重置：Agent.new_session 集中清空全部会话口径状态 ----------

def test_new_session_resets_all_session_scope_state(monkeypatch, tmp_path):
    """/new 的语义由 Agent.new_session 承担：会话级状态逐项清零，跨会话状态不动。

    覆盖点含曾经的遗漏项（工具侧「已读文件」记录、上下文真实 token 锚点）
    与既有各项：消息历史、会话用量、权限会话规则、越界信任目录、压缩计数、
    步骤清单。会话 id 轮换与 since_start 用量存活一并验证。"""
    from smithcode import plan
    from smithcode.tools import files as files_mod

    agent = _make_agent(monkeypatch)
    agent.session.usage.add({"prompt_tokens": 10, "completion_tokens": 5})
    agent.permission.session_rules.append(("run_command", "*", "allow"))
    config.SESSION_EXTRA_ROOTS.append(str(tmp_path))
    agent.context.compact_count = 3
    agent.context.last_actual = 1234
    files_mod.READ_FILES.add(str(tmp_path / "旧文件.py"))
    plan.current().replace([{"title": "步骤", "status": "pending"}])

    old_session_id = config.SESSION_ID
    agent.new_session()

    assert agent.session.messages == []
    assert agent.session.usage.current_session.calls == 0
    assert agent.session.usage.since_start.get("prompt_tokens") == 10  # 启动口径跨 /new 存活
    assert agent.permission.session_rules == []
    assert config.SESSION_EXTRA_ROOTS == []
    assert agent.context.compact_count == 0
    assert agent.context.last_actual is None  # 旧会话锚点对新会话无意义，作废
    assert not files_mod.READ_FILES
    assert not plan.has_active()
    assert config.SESSION_ID != old_session_id  # 会话 id 随重置轮换


# ---------- 用量统计：双口径累计与 /new 重置 ----------

def test_run_accumulates_usage(monkeypatch):
    """usage 事件应累计进会话双口径；/new 只清会话口径。"""

    class UsageLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            self.calls += 1
            yield (
                "usage",
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
            yield ("message", {"role": "assistant", "content": f"回复{self.calls}"})

    monkeypatch.setattr("smithcode.agent.LLMClient", UsageLLM)
    agent = Agent(session=Session())

    agent.run("第一条")
    agent.run("第二条")

    assert agent.session.usage.current_session.get("total_tokens") == 30
    assert agent.session.usage.since_start.get("total_tokens") == 30

    agent.session.reset()  # /new 语义：会话口径清零，启动口径保留
    assert agent.session.usage.current_session.get("total_tokens") == 0
    assert agent.session.usage.since_start.get("total_tokens") == 30


def test_run_without_usage_keeps_counters_clean(monkeypatch):
    """服务商不返回 usage（无 usage 事件）时，统计静默为空、主流程不受影响。"""
    agent = _make_agent(monkeypatch)
    agent.run("打个招呼")
    assert agent.session.usage.since_start.calls == 0


# ---------- 路径预检：授权目录之外的访问确认 ----------

def _outside_file(tmp_path):
    """构造一个授权目录之外的目标文件，返回 (目录, 路径参数)。"""
    outside = tmp_path.parent / (tmp_path.name + "-out")
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    return outside, str(outside / "secret.txt")


def _outside_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def _run_call(agent: Agent, call: dict) -> str:
    """跑单个工具调用并返回其结果文本（批量路径的单调用用法，结果在最后一条 tool 消息）。"""
    agent._execute_batch([call])
    return agent.session.messages[-1]["content"]


def test_tool_summary_flattened_to_single_line(monkeypatch, capsys):
    """摘要必须先压成单行再截断：run_command 的 describe 会拼进命令原文，
    多行命令（heredoc 等）的换行会把终端里的工具行撑成多行。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: False)

    command = "python - <<'PY'\nprint('hi')\nPY"
    _run_call(agent, _fake_tool_call("run_command", json.dumps({"command": command})))

    rows = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("⚙ ")]
    assert rows == ["⚙ command python - <<'PY' print('hi') PY"]


def test_execute_outside_path_denied(monkeypatch, tmp_path):
    """越界路径被用户拒绝时，工具不执行，返回统一的拒绝结果。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert _run_call(agent, call) == "用户拒绝了此操作"
    assert config.SESSION_EXTRA_ROOTS == []


def test_execute_outside_path_once_approval(monkeypatch, tmp_path):
    """[y] 仅本次：本次调用放行且拿到内容，但不留下会话级授权。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert "s" in _run_call(agent, call)
    assert config.SESSION_EXTRA_ROOTS == []


def test_execute_outside_path_always_approval(monkeypatch, tmp_path):
    """[a] 本会话总是：信任根入库，后续同目录访问不再询问。

    第二次调用前不再布置 input 的返回值——若预检再次询问，
    input 会抛 StopIteration 使测试失败。
    """
    outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "a")

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert "s" in _run_call(agent, call)
    assert config.SESSION_EXTRA_ROOTS == [str(outside)]

    assert "s" in _run_call(agent, call)


def test_execute_outside_path_auto_approved_with_yes(monkeypatch, tmp_path):
    """-y（approved_all）覆盖越界访问：静默放行本次调用，不弹确认、不留会话级信任。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    agent.permission.approved_all = True
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("不应弹出交互确认"))

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert "s" in _run_call(agent, call)
    assert config.SESSION_EXTRA_ROOTS == []  # "仅本次"语义


def test_execute_outside_path_denied_non_interactive(monkeypatch, tmp_path):
    """非交互 stdin 下越界访问直接拒绝，不调用 input、不因 EOFError 崩溃。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("非交互不应调用 input"))

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert _run_call(agent, call) == "用户拒绝了此操作"


# ---------- apply_patch：多路径工具流程 ----------

def _patch_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def test_execute_apply_patch_creates_file(monkeypatch, tmp_path):
    """apply_patch 走多路径流程：聚合权限（edit_file 族 ask）确认后执行并落盘。"""
    agent = _patch_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    call = _fake_tool_call("apply_patch", json.dumps({"patch": "*** Add File: hi.txt\n+hi\n"}))
    assert "已应用" in _run_call(agent, call)
    assert (tmp_path / "hi.txt").read_text(encoding="utf-8") == "hi"


def test_execute_apply_patch_denied_for_git(monkeypatch, tmp_path):
    """apply_patch 触及 .git（继承 edit_file 的 deny 保护）直接拒绝，不弹确认、不落盘。"""
    agent = _patch_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("deny 不应弹交互确认"))

    call = _fake_tool_call(
        "apply_patch",
        json.dumps({"patch": "*** Add File: .git/hooks/pre-commit\n+echo x\n"}),
    )
    assert _run_call(agent, call) == "用户拒绝了此操作"
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()


# ---------- Esc 中断：流式截停与工具批占位 ----------

def test_interrupt_during_stream_keeps_partial_content(monkeypatch):
    """流中取消：已收到的正文拼成部分消息入库，任务以 interrupted 结束。"""

    class InterruptingLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield ("content", "部分")
            agent.interrupt()  # 模拟用户按 Esc
            yield ("content", "输出")
            # 真实实现里流在此截停，不再产出 message

    monkeypatch.setattr("smithcode.agent.LLMClient", InterruptingLLM)
    agent = Agent(session=Session())

    result = agent.run("写首诗")
    assert result.status == "interrupted"
    assert result.partial is True
    msgs = agent.session.messages
    assert msgs[-2]["role"] == "assistant"
    assert msgs[-2]["content"] == "部分输出"
    # 中断事件回写上下文：末条是给模型看的 user 注释（不触发新请求）
    assert msgs[-1]["role"] == "user"
    assert "中断" in msgs[-1]["content"]


def test_interrupt_writes_context_note_without_new_call(monkeypatch):
    """中断回写：会话末尾追加一条 user 注释、不因此再发起请求，下一轮可见。"""
    calls = []

    class InterruptingLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            calls.append(list(messages))
            yield ("content", "部分")
            agent.interrupt()

    monkeypatch.setattr("smithcode.agent.LLMClient", InterruptingLLM)
    agent = Agent(session=Session())

    result = agent.run("做点事")
    assert result.status == "interrupted"
    assert len(calls) == 1  # 回写上下文没有触发新的模型调用
    note = agent.session.messages[-1]
    assert note["role"] == "user"
    assert "中断" in note["content"]

    # 下一轮提问：注释仍在历史里（模型可见），新输入追加在其后
    agent.llm = FakeLLM()
    agent.run("继续")
    assert any("中断" in m.get("content", "") for m in agent.session.messages)


def test_interrupt_before_tool_batch_fills_placeholders(monkeypatch):
    """流结束后才取消：这批 tool_calls 全部未执行，一律补占位（防悬空 tool_call_id）。"""

    class StreamThenCancelLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_fake_tool_call("list_dir", "{}"),
                                   _fake_tool_call("list_dir", "{}")],
                },
            )
            agent.interrupt()

    monkeypatch.setattr("smithcode.agent.LLMClient", StreamThenCancelLLM)
    agent = Agent(session=Session())

    result = agent.run("中断测试")
    assert result.status == "interrupted"
    tool_msgs = [m for m in agent.session.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert all("未执行" in m["content"] for m in tool_msgs)


def test_interrupt_mid_batch_stops_remaining(monkeypatch):
    """工具批执行中取消：已执行的正常入库，未执行的补占位、不再发起。"""
    executed = []

    def step_tool():
        agent.interrupt()
        executed.append(True)
        return "第一步完成"

    monkeypatch.setitem(FUNCTIONS, "step_tool", step_tool)
    monkeypatch.setattr(config, "MAX_TOOL_CONCURRENCY", 1)  # 纯串行路径，结果确定

    class ToolThenCancelLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_fake_tool_call("step_tool", "{}"),
                                   _fake_tool_call("list_dir", "{}")],
                },
            )

    monkeypatch.setattr("smithcode.agent.LLMClient", ToolThenCancelLLM)
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: True)

    result = agent.run("中断批处理")
    assert result.status == "interrupted"
    assert executed == [True]  # list_dir 未再执行
    tool_msgs = [m for m in agent.session.messages if m["role"] == "tool"]
    assert "第一步完成" in tool_msgs[0]["content"]
    assert "未执行" in tool_msgs[1]["content"]


def test_interrupt_during_preflight_skips_remaining(monkeypatch):
    """预检阶段取消：剩余 tool_calls 不再预检、不再弹确认框，一律补占位。

    已确认但未执行的第 1 个计划同样跳过——中断意味着不再发起任何新工作。
    """
    asked = []
    executed = []

    def step_tool():
        executed.append(True)
        return "第一步完成"

    def check(name, args, content=None):
        if not asked:
            agent.interrupt()  # 模拟确认第 1 个工具时用户按 Esc
        asked.append(name)
        return True

    monkeypatch.setitem(FUNCTIONS, "step_tool", step_tool)
    monkeypatch.setattr(config, "MAX_TOOL_CONCURRENCY", 1)

    class ToolThenCancelLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_fake_tool_call("step_tool", "{}"),
                                   _fake_tool_call("step_tool", "{}")],
                },
            )

    monkeypatch.setattr("smithcode.agent.LLMClient", ToolThenCancelLLM)
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", check)

    result = agent.run("中断预检")
    assert result.status == "interrupted"
    assert asked == ["step_tool"]  # 第 2 个未再预检（确认框只弹过一次）
    assert executed == []  # 已确认的也未执行
    tool_msgs = [m for m in agent.session.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert all("未执行" in m["content"] for m in tool_msgs)


def test_interrupt_during_confirmation_overrides_denied(monkeypatch):
    """权限确认期间中断：即使随后答 n（本会 denied），也按 interrupted 收尾、不执行。"""
    executed = []

    def step_tool():
        executed.append(True)
        return "不应执行"

    def check(name, args, content=None):
        agent.interrupt()  # 确认框弹出期间用户按 Esc
        return False       # 随后答 n——按中断语义优先，不转 denied

    monkeypatch.setitem(FUNCTIONS, "step_tool", step_tool)

    class OneToolLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [_fake_tool_call("step_tool", "{}")],
                },
            )

    monkeypatch.setattr("smithcode.agent.LLMClient", OneToolLLM)
    agent = Agent(session=Session())
    monkeypatch.setattr(agent.permission, "check", check)

    result = agent.run("确认中中断")
    assert result.status == "interrupted"
    assert executed == []
    tool_msgs = [m for m in agent.session.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "未执行" in tool_msgs[0]["content"]


def test_session_reusable_after_interrupt(monkeypatch):
    """中断后令牌已复位：同一会话继续追问正常工作，不受残留取消状态影响。"""

    class InterruptingLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            yield ("content", "部分")
            agent.interrupt()

    monkeypatch.setattr("smithcode.agent.LLMClient", InterruptingLLM)
    agent = Agent(session=Session())
    agent.run("第一条")

    agent.llm = FakeLLM()  # 换回正常假 LLM（Agent 构造时已绑定实例，事后 patch 类不生效）
    result = agent.run("继续")
    assert result.status == "ok"
    assert result.text == "最终回复"


def test_run_with_skill_returns_body_in_tool_result(monkeypatch, tmp_path):
    """端到端：目录进首轮系统提示词；use_skill 的正文作为工具结果进历史。"""
    from smithcode import skills

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    skill_dir = workspace / ".agents" / "skills" / "proj"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: proj\ndescription: 测试技能\n---\nBODY-MARKER\n", encoding="utf-8"
    )

    class SkillLLM:
        def __init__(self):
            self.requests = []
            self.tools = []

        def chat_stream(self, messages, tools=None):
            self.requests.append([dict(m) for m in messages])
            self.tools.append(tools)
            if len(self.requests) == 1:
                yield (
                    "message",
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            _fake_tool_call("use_skill", json.dumps({"name": "proj"}))
                        ],
                    },
                )
            else:
                yield ("message", {"role": "assistant", "content": "完成"})

    skills.clear()
    try:
        monkeypatch.setattr("smithcode.agent.LLMClient", SkillLLM)
        agent = Agent(session=Session())
        agent.refresh_skills()

        result = agent.run("用技能处理")

        assert result.status == "ok"
        first_prompt = agent.llm.requests[0][0]["content"]
        assert "## 可用技能" in first_prompt
        assert any(s["name"] == "use_skill" for s in agent.llm.tools[0])
        second_prompt = agent.llm.requests[1][0]["content"]
        assert second_prompt == first_prompt  # 加载技能不改动系统提示词
        tool_results = [m for m in agent.llm.requests[1] if m.get("role") == "tool"]
        assert tool_results and "BODY-MARKER" in tool_results[-1]["content"]
    finally:
        skills.clear()


# ---------- 响应流中途断开：重试预算用尽后的收尾 ----------
#
# 这里的假 LLM 直接替换 `LLMClient`，因此**不含客户端内部的重试循环**——覆盖的是
# "重试用尽之后 Agent 怎么收尾"。"重试成功后两段正文同入一条消息"由
# `test_retry_accumulates_both_attempts_in_one_message` 用真实客户端验证。

class MidStreamTimeoutLLM:
    """先正常吐一段正文，再抛读完超时（重试预算耗尽前的每次尝试都如此）。"""

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        yield ("content", "已修改完成，")
        yield ("content", "总结如下：")
        raise httpx2.ReadTimeout("The read operation timed out")


def test_stream_timeout_keeps_partial_in_history(monkeypatch):
    """重试用尽：状态为 stream_error，已上屏的部分正文必须写进会话历史。

    否则下一轮模型看不到自己说过什么，会从头重做、再复述一遍（原始故障现象）。
    中断说明还要带上失败原因——只报「中断」不报为什么断，用户无从排障。
    """
    monkeypatch.setattr("smithcode.agent.LLMClient", MidStreamTimeoutLLM)
    agent = Agent(session=Session())

    result = agent.run("改一下")

    assert result.status == "stream_error"
    assert result.text == "已修改完成，总结如下："
    assert result.reason.startswith("读取超时")  # TUI 页脚 / 控制台据此展示原因
    contents = [m.get("content") for m in agent.session.messages]
    assert "已修改完成，总结如下：" in contents  # partial 落库
    note = contents[-1]
    assert "读取超时" in note  # 原因写进下一轮可见的中断说明
    assert "请基于它继续完成任务，不要从头重做。" in note  # 续写引导仍在
    assert [m["role"] for m in agent.session.messages][-2:] == ["assistant", "user"]


def test_stream_timeout_before_content_leaves_no_empty_assistant(monkeypatch):
    """首块之前就断开：不留空 assistant 消息，只补一行中断说明。"""
    class FailFirstTokenLLM:
        def chat_stream(self, messages, tools=None):
            raise httpx2.ReadTimeout("The read operation timed out")
            yield  # pragma: no cover 生成器语义需要

    monkeypatch.setattr("smithcode.agent.LLMClient", FailFirstTokenLLM)
    agent = Agent(session=Session())

    result = agent.run("改一下")

    assert result.status == "stream_error"
    assert result.text == ""
    roles = [m["role"] for m in agent.session.messages]
    assert "assistant" not in roles  # 没有空 assistant 消息
    assert roles == ["system", "user", "user"]


def test_stream_timeout_does_not_record_partial_twice(monkeypatch):
    """上下文溢出恢复后才断开：partial 只入库一次（恢复层与 run 层不重复记账）。"""
    class OverflowThenTimeoutLLM:
        def __init__(self):
            self.calls = 0

        def chat_stream(self, messages, tools=None):
            if messages[0].get("content", "").startswith("你是上下文压缩器"):
                yield ("message", {"role": "assistant", "content": "## 目标\n压缩旧历史\n## 下一步\n继续"})
                return
            self.calls += 1
            if self.calls == 1:
                # 措辞要能被 is_context_overflow 认出，才走恢复路径
                raise RuntimeError("This model's maximum context length is 4096 tokens")
            yield ("content", "部分总结")
            raise httpx2.ReadTimeout("The read operation timed out")

    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 5000)  # 不触发预检，纯溢出恢复
    monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 150)
    monkeypatch.setattr("smithcode.agent.LLMClient", OverflowThenTimeoutLLM)
    session = Session()
    # 造出可压缩的中段，让恢复路径真的压缩一次再重试
    session.messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "旧任务"},
        {"role": "assistant", "content": "旧回复" * 200},
        {"role": "user", "content": "再问一次"},
        {"role": "assistant", "content": "再答一次" * 200},
    ]
    agent = Agent(session=session)

    result = agent.run("改一下")

    assert result.status == "stream_error"
    assert result.text == "部分总结"
    contents = [m.get("content") for m in agent.session.messages]
    assert contents.count("部分总结") == 1  # 只落一次


class _RecordingView:
    """只记录重试事件的最小渲染后端。"""

    def __init__(self):
        self.retries = []
        self.finished = 0

    def retry_started(self, state, owner=None):
        self.retries.append(state)

    def retry_finished(self, owner=None):
        self.finished += 1

    def __getattr__(self, name):  # 其余事件忽略
        return lambda *a, **k: None


def test_retry_accumulates_both_attempts_in_one_message(monkeypatch):
    """重试成功后：两次尝试的正文按顺序都在同一条 assistant 消息里（对齐 opencode）。

    真实客户端（含重试循环）跑这条链，只把 `_stream_once` 换成脚本化的流。
    屏幕上两段内容都出现过，历史里就必须都有——否则下一轮模型看到的历史与用户
    看到的屏幕不一致（原始故障的另一半）。
    """
    from smithcode.llm.client import LLMClient

    calls = []

    def stream_once(kwargs):
        calls.append(1)
        if len(calls) == 1:
            yield ("content", "已修改完成，")
            raise httpx2.ReadTimeout("The read operation timed out")
        yield ("content", "总结如下：改了 commands/base.py。")
        yield ("message", {"role": "assistant", "content": ""})

    client = LLMClient(api_key="test", base_url=None, timeout=1.0, default_model="m",
                       client_factory=lambda **kwargs: SimpleNamespace())
    client._stream_once = stream_once
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: client)
    monkeypatch.setattr("smithcode.llm.retry.wait", lambda state: None)  # 不真等退避
    view = _RecordingView()
    monkeypatch.setattr("smithcode.renderer.current", lambda: view)

    agent = Agent(session=Session())
    result = agent.run("改成只读面板")

    assert result.status == "ok"
    assert result.text == "已修改完成，总结如下：改了 commands/base.py。"  # 两段拼接
    assert len(calls) == 2
    assert len(view.retries) == 1 and view.finished == 1
    assistant = [m for m in agent.session.messages if m["role"] == "assistant"]
    assert len(assistant) == 1  # 一条消息，不是两条
    assert assistant[0]["content"] == "已修改完成，总结如下：改了 commands/base.py。"


def test_format_stream_interrupted_carries_reason_and_timeout_hint(monkeypatch):
    """「输出中断」必须带出失败原因；读超时额外给出可操作提示。

    只报「输出中断」而不报为什么断，用户与排查者都无从下手——这正是原始
    故障里最难定位的一点。
    """
    from smithcode.agent import format_stream_interrupted

    monkeypatch.setattr(config, "LLM_TIMEOUT", 120)
    timeout_text = format_stream_interrupted("读取超时: The read operation timed out")
    assert "读取超时" in timeout_text
    assert "read operation timed out" in timeout_text
    assert "llm_timeout" in timeout_text and "120" in timeout_text  # 可操作提示

    other = format_stream_interrupted("请求过于频繁: 429")
    assert "请求过于频繁" in other
    assert "llm_timeout" not in other  # 非超时不给超时提示

    assert format_stream_interrupted("") == ""
    assert format_stream_interrupted(None) == ""


def test_renderer_failure_is_not_reported_as_stream_error(monkeypatch):
    """渲染后端抛异常时不得被当成「输出中断」（UI 故障 vs 网络故障必须区分）。

    回归用户实际遇到的现象：`Relay` 广播未知事件给标题呈现器时抛
    `AttributeError`，被流异常处理捕获后每一轮都报 stream_error，把 UI 故障
    描述成网络中断、排查方向直接跑偏。
    """
    from smithcode.agent import RendererError

    class OkStreamLLM:
        def chat_stream(self, messages, tools=None):
            yield ("content", "你好！")
            yield ("message", {"role": "assistant", "content": "你好！"})

    class BrokenRenderer(_RecordingView):
        def stream(self, kind, chunk):
            raise AttributeError("'TerminalTitlePresenter' object has no attribute 'x'")

    monkeypatch.setattr("smithcode.agent.LLMClient", OkStreamLLM)
    broken = BrokenRenderer()
    monkeypatch.setattr("smithcode.renderer.current", lambda: broken)
    agent = Agent(session=Session())

    with pytest.raises(RendererError) as err:
        agent.run("hello")

    assert "渲染后端异常" in str(err.value)
    assert "AttributeError" in str(err.value)
    roles = [m["role"] for m in agent.session.messages]
    assert roles == ["system", "user"]  # 没有 partial 入库、没有中断说明

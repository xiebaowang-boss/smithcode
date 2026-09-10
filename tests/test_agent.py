"""Agent 主循环测试：用假 LLM 验证流式消费、循环与终止逻辑，不依赖真实 API。"""

import json

import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.context import truncate_output
from smithcode.session import Session
from smithcode.tools import FUNCTIONS


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.confirmations_available", lambda: True)


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


def test_agent_loop_stops_at_max_iterations(monkeypatch):
    class ToolCallLoopLLM(FakeLLM):
        """永远要求调用工具，用于验证最大迭代保护。"""

        def chat_stream(self, messages, tools=None):
            yield (
                "message",
                {"role": "assistant", "content": "", "tool_calls": [_fake_tool_call()]},
            )

    monkeypatch.setattr("smithcode.agent.LLMClient", ToolCallLoopLLM)
    agent = Agent(session=Session(), max_iterations=2)
    assert agent.run("死循环").text == "达到最大迭代次数，任务中止。"


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
    monkeypatch.setattr(agent.permission, "check", lambda name, args: True)

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
    monkeypatch.setattr("smithcode.permission.confirmations_available", lambda: False)
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
    assert agent.session.messages[-1]["role"] == "assistant"
    assert agent.session.messages[-1]["content"] == "部分输出"


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
    monkeypatch.setattr(agent.permission, "check", lambda name, args: True)

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

    def check(name, args):
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

    def check(name, args):
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

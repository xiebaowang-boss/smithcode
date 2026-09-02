"""Agent 主循环测试：用假 LLM 验证流式消费、循环与终止逻辑，不依赖真实 API。"""

import json

import pytest

from codeagent import config
from codeagent.agent import Agent, truncate_output
from codeagent.session import Session
from codeagent.tools import FUNCTIONS


@pytest.fixture(autouse=True)
def enable_prompting(monkeypatch):
    """pytest 环境下 stdin 非 TTY，显式放行交互确认，否则权限确认会全部 fail-closed 拒绝。"""
    monkeypatch.setattr("codeagent.permission.confirmations_available", lambda: True)


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
    monkeypatch.setattr("codeagent.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def test_run_returns_final_content(monkeypatch):
    agent = _make_agent(monkeypatch)
    assert agent.run("打个招呼") == "最终回复"


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

    monkeypatch.setattr("codeagent.agent.LLMClient", ToolCallLoopLLM)
    agent = Agent(session=Session(), max_iterations=2)
    assert agent.run("死循环") == "达到最大迭代次数，任务中止。"


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

    monkeypatch.setattr("codeagent.agent.LLMClient", OneToolThenDoneLLM)
    monkeypatch.setitem(FUNCTIONS, "big_tool", lambda: "x" * 5000)
    # 把上限调小，避免测试里塞几万字符
    monkeypatch.setattr("codeagent.config.MAX_TOOL_OUTPUT", 1000)
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

    monkeypatch.setattr("codeagent.agent.LLMClient", ReasoningLLM)
    agent = Agent(session=Session())

    assert agent.run("打个招呼") == "你好"

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

    monkeypatch.setattr("codeagent.agent.LLMClient", ThinkThenAnswerLLM)
    agent = Agent(session=Session())

    msg, usage = agent._chat()
    assert msg == {"role": "assistant", "content": "答案"}
    assert usage is None  # 假 LLM 没发 usage 事件

    out = capsys.readouterr().out
    assert "\x1b[90m[Thinking] 想一想\x1b[0m\n助手> 答案" in out


# ---------- 用量统计：双口径累计与 /new 重置 ----------

def test_run_accumulates_usage(monkeypatch):
    """usage 事件应累计进会话双口径，并作为 last_usage 供 CLI 展示。"""

    class UsageLLM(FakeLLM):
        def chat_stream(self, messages, tools=None):
            self.calls += 1
            yield (
                "usage",
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
            yield ("message", {"role": "assistant", "content": f"回复{self.calls}"})

    monkeypatch.setattr("codeagent.agent.LLMClient", UsageLLM)
    agent = Agent(session=Session())

    agent.run("第一条")
    agent.run("第二条")

    assert agent.last_usage.get("total_tokens") == 15  # last_usage 只含最后一轮
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
    assert agent.last_usage.calls == 0


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
    monkeypatch.setattr("codeagent.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def test_execute_outside_path_denied(monkeypatch, tmp_path):
    """越界路径被用户拒绝时，工具不执行，返回统一的拒绝结果。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "n")

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert agent._execute(call) == "用户拒绝了此操作"
    assert config.SESSION_EXTRA_ROOTS == []


def test_execute_outside_path_once_approval(monkeypatch, tmp_path):
    """[y] 仅本次：本次调用放行且拿到内容，但不留下会话级授权。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert agent._execute(call) == "s"
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
    assert agent._execute(call) == "s"
    assert config.SESSION_EXTRA_ROOTS == [str(outside)]

    assert agent._execute(call) == "s"


def test_execute_outside_path_auto_approved_with_yes(monkeypatch, tmp_path):
    """-y（approved_all）覆盖越界访问：静默放行本次调用，不弹确认、不留会话级信任。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    agent.permission.approved_all = True
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("不应弹出交互确认"))

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert agent._execute(call) == "s"
    assert config.SESSION_EXTRA_ROOTS == []  # "仅本次"语义


def test_execute_outside_path_denied_non_interactive(monkeypatch, tmp_path):
    """非交互 stdin 下越界访问直接拒绝，不调用 input、不因 EOFError 崩溃。"""
    _outside, arg = _outside_file(tmp_path)
    agent = _outside_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("codeagent.permission.confirmations_available", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("非交互不应调用 input"))

    call = _fake_tool_call("read_file", json.dumps({"path": arg}))
    assert agent._execute(call) == "用户拒绝了此操作"


# ---------- apply_patch：多路径工具流程 ----------

def _patch_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(config, "SESSION_EXTRA_ROOTS", [])
    monkeypatch.setattr("codeagent.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def test_execute_apply_patch_creates_file(monkeypatch, tmp_path):
    """apply_patch 走多路径流程：聚合权限（edit_file 族 ask）确认后执行并落盘。"""
    agent = _patch_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: "y")

    call = _fake_tool_call("apply_patch", json.dumps({"patch": "*** Add File: hi.txt\n+hi\n"}))
    result = agent._execute(call)
    assert "已应用" in result
    assert (tmp_path / "hi.txt").read_text(encoding="utf-8") == "hi"


def test_execute_apply_patch_denied_for_git(monkeypatch, tmp_path):
    """apply_patch 触及 .git（继承 edit_file 的 deny 保护）直接拒绝，不弹确认、不落盘。"""
    agent = _patch_agent(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("deny 不应弹交互确认"))

    call = _fake_tool_call(
        "apply_patch",
        json.dumps({"patch": "*** Add File: .git/hooks/pre-commit\n+echo x\n"}),
    )
    assert agent._execute(call) == "用户拒绝了此操作"
    assert not (tmp_path / ".git" / "hooks" / "pre-commit").exists()

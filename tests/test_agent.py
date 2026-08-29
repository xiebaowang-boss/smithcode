"""Agent 主循环测试：用假 LLM 验证流式消费、循环与终止逻辑，不依赖真实 API。"""

from codeagent.agent import Agent, truncate_output
from codeagent.session import Session
from codeagent.tools import FUNCTIONS


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

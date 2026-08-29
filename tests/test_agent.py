"""Agent 主循环测试：用假 LLM 验证循环与终止逻辑，不依赖真实 API。"""
from types import SimpleNamespace

from codeagent.agent import Agent
from codeagent.session import Session


def _fake_tool_call(name="list_dir", args="{}"):
    """模拟 OpenAI 返回的 tool_calls 对象（属性访问方式与真实 API 一致）。"""
    return SimpleNamespace(
        id="1",
        type="function",
        function=SimpleNamespace(name=name, arguments=args),
    )


class FakeMessage:
    def __init__(self, content="你好", tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


class FakeLLM:
    """单轮即返回最终回复的假 LLM。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return FakeMessage("最终回复")


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

        def chat(self, messages, tools=None):
            return FakeMessage(
                "",
                tool_calls=[_fake_tool_call()],
            )

    monkeypatch.setattr("codeagent.agent.LLMClient", ToolCallLoopLLM)
    agent = Agent(session=Session(), max_iterations=2)
    assert agent.run("死循环") == "达到最大迭代次数，任务中止。"

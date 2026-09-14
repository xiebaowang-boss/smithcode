"""Agent × MCP 集成测试：工具进入请求 schema、调用执行与默认权限语义。"""

import json
import sys
from pathlib import Path

import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.mcp import config as mcp_config
from smithcode.mcp import secrets
from smithcode.session import Session
from smithcode.tools import DYNAMIC, unregister_dynamic, visible_schemas

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    secrets.redactor().clear()
    yield workspace, home
    for name in list(DYNAMIC):
        unregister_dynamic(name)
    secrets.redactor().clear()


def _fake_tool_call(name, args):
    return {
        "id": "1",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


class _McpLLM:
    """第一轮调用 MCP 工具，第二轮收尾。"""

    def __init__(self):
        self.calls = 0
        self.seen_tools = None

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            self.seen_tools = tools
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        _fake_tool_call("mcp__fake__echo", json.dumps({"msg": "hi"}))
                    ],
                },
            )
        else:
            yield ("message", {"role": "assistant", "content": "done"})


def _prepare(monkeypatch):
    mcp_config.write_user_server(
        mcp_config.ServerConfig(
            name="fake",
            command=[sys.executable, str(FIXTURE)],
            env={"FAKE_MCP_MODE": "default"},
            timeout=10.0,
        )
    )
    monkeypatch.setattr("smithcode.agent.LLMClient", _McpLLM)
    agent = Agent(session=Session())
    agent.mcp.start()
    assert agent.mcp.wait(15)
    return agent


def test_mcp_tool_visible_and_executed(monkeypatch):
    agent = _prepare(monkeypatch)
    try:
        assert agent.mcp.status()[0].state == "connected"
        agent.permission.user_rules = [("mcp__fake__echo", "*", "allow")]

        result = agent.run("call echo")

        assert result.status == "ok"
        names = [tool["name"] for tool in agent.llm.seen_tools]
        assert "mcp__fake__echo" in names
        tool_messages = [m for m in agent.session.messages if m["role"] == "tool"]
        assert "echo: hi" in tool_messages[-1]["content"]
        assert "mcp__fake__echo" in result.tools_used
    finally:
        agent.mcp.stop()


def test_mcp_tool_requires_confirmation_by_default(monkeypatch):
    agent = _prepare(monkeypatch)
    try:
        # 测试环境 stdin 非 TTY：ask 走 fail-closed 拒绝，任务终止
        result = agent.run("call echo")

        assert result.status == "denied"
        tool_messages = [m for m in agent.session.messages if m["role"] == "tool"]
        assert "拒绝" in tool_messages[-1]["content"]
    finally:
        agent.mcp.stop()


def test_stop_unregisters_tools(monkeypatch):
    agent = _prepare(monkeypatch)
    assert "mcp__fake__echo" in {s["name"] for s in visible_schemas()}
    agent.mcp.stop()
    assert "mcp__fake__echo" not in {s["name"] for s in visible_schemas()}

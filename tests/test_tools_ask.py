"""ask_user 工具测试：交互提问、非交互 fail-closed、空回答、权限放行。"""
import pytest

from codeagent import config
from codeagent.permission import evaluate
from codeagent.tools.ask import ask_user


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    """默认放行交互确认（pytest 的 stdin 非 TTY）。"""
    monkeypatch.setattr("codeagent.tools.ask.confirmations_available", lambda: True)


def test_ask_user_returns_answer(monkeypatch, capsys):
    """交互模式下打印问题并返回用户回答。"""
    monkeypatch.setattr("codeagent.tools.ask.read_user_input", lambda prompt="回答> ": "是的")
    assert ask_user("要继续吗？") == "是的"
    assert "[提问] 要继续吗？" in capsys.readouterr().out


def test_ask_user_empty_answer(monkeypatch):
    """空白回答归一化为占位说明。"""
    monkeypatch.setattr("codeagent.tools.ask.read_user_input", lambda prompt="回答> ": "   ")
    assert ask_user("问题") == "（用户未输入内容）"


def test_ask_user_fail_closed_non_interactive(monkeypatch, capsys):
    """非交互 stdin 下不阻塞等待，返回已取消提示。"""
    monkeypatch.setattr("codeagent.tools.ask.confirmations_available", lambda: False)
    monkeypatch.setattr(
        "codeagent.tools.ask.read_user_input",
        lambda prompt="回答> ": pytest.fail("非交互不应读取输入"),
    )
    assert "无法向用户提问" in ask_user("问题")


def test_ask_user_uses_shared_read_user_input():
    """ask_user 与 REPL 共用同一个 read_user_input（多行粘贴合并逻辑）。"""
    from codeagent.tools import ask as ask_module
    from codeagent.utils.terminal import read_user_input

    assert ask_module.read_user_input is read_user_input


def test_ask_user_allowed_by_default():
    """ask_user 默认规则为 allow（提问不再弹确认）。"""
    assert evaluate("ask_user", "*", [("ask_user", "*", "allow")])[2] == "allow"
    assert evaluate("ask_user", "任意问题", [])[0] == "ask_user"


def test_ask_user_can_be_denied():
    """用户可用 deny 规则禁用 ask_user（如 CI 强制不提问）。"""
    from codeagent.permission import Permission

    perm = Permission()
    perm.user_rules = [("ask_user", "*", "deny")]
    assert perm.check("ask_user", {"question": "x"}) is False


def test_ask_user_works_in_agent_loop(monkeypatch, tmp_path):
    """Agent 循环里 ask_user 作为普通工具执行，回答以工具结果回传。"""
    import json

    from codeagent.agent import Agent
    from codeagent.session import Session

    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("codeagent.agent.LLMClient", type("DummyLLM", (), {}))
    monkeypatch.setattr("codeagent.tools.ask.confirmations_available", lambda: True)
    monkeypatch.setattr("codeagent.tools.ask.read_user_input", lambda prompt="回答> ": "继续")

    agent = Agent(session=Session())
    call = {"function": {"name": "ask_user", "arguments": json.dumps({"question": "确认？"})}}
    assert agent._execute(call) == "继续"
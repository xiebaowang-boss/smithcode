"""ask_user 工具测试：交互提问、非交互 fail-closed、空回答、权限放行。"""
import pytest

from smithcode import config
from smithcode.permission import evaluate
from smithcode.tools.ask import ask_user


@pytest.fixture(autouse=True)
def interactive(monkeypatch):
    """默认放行交互确认（pytest 的 stdin 非 TTY）。"""
    monkeypatch.setattr("smithcode.tools.ask.confirmations_available", lambda: True)


def test_ask_user_returns_answer(monkeypatch, capsys):
    """交互模式下打印问题并返回用户回答。"""
    monkeypatch.setattr("smithcode.renderer.read_user_input", lambda prompt="回答> ": "是的")
    assert ask_user("要继续吗？") == "是的"
    assert "[提问] 要继续吗？" in capsys.readouterr().out


def test_ask_user_empty_answer(monkeypatch):
    """空白回答归一化为占位说明。"""
    monkeypatch.setattr("smithcode.renderer.read_user_input", lambda prompt="回答> ": "   ")
    assert ask_user("问题") == "（用户未输入内容）"


def test_ask_user_fail_closed_non_interactive(monkeypatch, capsys):
    """非交互 stdin 下不阻塞等待，返回已取消提示。"""
    monkeypatch.setattr("smithcode.tools.ask.confirmations_available", lambda: False)
    monkeypatch.setattr(
        "smithcode.renderer.read_user_input",
        lambda prompt="回答> ": pytest.fail("非交互不应读取输入"),
    )
    assert "无法向用户提问" in ask_user("问题")


def test_ask_user_routes_through_console_renderer():
    """ask_user 与 REPL 共用同一个渲染后端（当前为 ConsoleRenderer）。"""
    from smithcode import renderer

    assert isinstance(renderer.current(), renderer.ConsoleRenderer)


def test_ask_user_allowed_by_default():
    """ask_user 默认规则为 allow（提问不再弹确认）。"""
    assert evaluate("ask_user", "*", [("ask_user", "*", "allow")])[2] == "allow"
    assert evaluate("ask_user", "任意问题", [])[0] == "ask_user"


def test_ask_user_can_be_denied():
    """用户可用 deny 规则禁用 ask_user（如 CI 强制不提问）。"""
    from smithcode.permission import Permission

    perm = Permission()
    perm.user_rules = [("ask_user", "*", "deny")]
    assert perm.check("ask_user", {"question": "x"}) is False


def test_ask_user_works_in_agent_loop(monkeypatch, tmp_path):
    """Agent 循环里 ask_user 作为普通工具执行，回答以工具结果回传。"""
    import json

    from smithcode.agent import Agent
    from smithcode.session import Session

    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("smithcode.agent.LLMClient", type("DummyLLM", (), {}))
    monkeypatch.setattr("smithcode.tools.ask.confirmations_available", lambda: True)
    monkeypatch.setattr("smithcode.renderer.read_user_input", lambda prompt="回答> ": "继续")

    agent = Agent(session=Session())
    call = {"function": {"name": "ask_user", "arguments": json.dumps({"question": "确认？"})}}
    assert agent._execute(call)[0] == "继续"


# ---------- 选项提问（opencode 式） ----------


def _inputs(monkeypatch, answers):
    """依次返回预设输入的 read_user_input 替身。"""
    seq = list(answers)
    monkeypatch.setattr("smithcode.renderer.read_user_input", lambda prompt="": seq.pop(0))


def test_ask_choice_single_pick(monkeypatch, capsys):
    """单选：编号即答，返回所选项。"""
    from smithcode.renderer import ConsoleRenderer

    _inputs(monkeypatch, ["2"])
    out = ConsoleRenderer().ask_choice("用哪个？", ["A", "B"], False)
    assert out == "B"
    text = capsys.readouterr().out
    assert "[提问] 用哪个？" in text and "1. A" in text and "2. B" in text


def test_ask_choice_custom_text(monkeypatch):
    """直接输入非编号文本 = 自定义回答原样返回。"""
    from smithcode.renderer import ConsoleRenderer

    _inputs(monkeypatch, ["改成紫色"])
    assert ConsoleRenderer().ask_choice("颜色？", ["红", "蓝"], False) == "改成紫色"


def test_ask_choice_multiple_numbers(monkeypatch):
    """多选：逗号分隔编号，返回逗号拼接的 label。"""
    from smithcode.renderer import ConsoleRenderer

    _inputs(monkeypatch, ["1,3"])
    assert ConsoleRenderer().ask_choice("吃啥？", ["面", "饭", "粥"], True) == "面, 粥"


def test_ask_choice_empty_cancels(monkeypatch):
    """空输入 = 取消，返回空串由调用方兜底。"""
    from smithcode.renderer import ConsoleRenderer

    _inputs(monkeypatch, [""])
    assert ConsoleRenderer().ask_choice("确定？", ["是", "否"], False) == ""


def test_ask_choice_invalid_number_reasks(monkeypatch, capsys):
    """超范围编号提示无效并重新询问。"""
    from smithcode.renderer import ConsoleRenderer

    _inputs(monkeypatch, ["9", "1"])
    assert ConsoleRenderer().ask_choice("选一个", ["A", "B"], False) == "A"
    assert "无效编号" in capsys.readouterr().out


def test_ask_user_with_options_routes_to_choice(monkeypatch):
    """带 options 的 ask_user 走 ask_choice，返回所选项。"""

    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    current = renderer.current()
    monkeypatch.setattr(
        current, "ask_choice", lambda q, opts, multiple=False: f"picked:{opts[1]}"
    )
    answer = ask_mod.ask_user("？", [{"label": "甲"}, {"label": "乙"}])
    assert answer == "picked:乙"


def test_ask_user_without_options_uses_text(monkeypatch):
    """不带 options 的 ask_user 走纯文本提问（老行为）。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    current = renderer.current()
    calls = []

    def fake_ask_text(question):
        calls.append(question)
        return "自由回答"

    monkeypatch.setattr(current, "ask_text", fake_ask_text)
    assert ask_mod.ask_user("问啥？") == "自由回答"
    assert calls == ["问啥？"]


def test_ask_user_multiple_flag_passed_through(monkeypatch):
    """multiple 标志透传给 ask_choice。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    seen = {}

    def fake_ask_choice(question, options, multiple=False):
        seen["multiple"] = multiple
        return "A, B"

    monkeypatch.setattr(renderer.current(), "ask_choice", fake_ask_choice)
    assert ask_mod.ask_user("？", [{"label": "A"}, {"label": "B"}], multiple=True) == "A, B"
    assert seen["multiple"] is True
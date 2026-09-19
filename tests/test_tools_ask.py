"""ask_user 工具测试：复数入参归一化、单/多题回传、交互提问、非交互 fail-closed、
空回答、权限放行。"""
import asyncio

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
    assert ask_user([{"question": "要继续吗？"}]) == "是的"
    assert "[提问] 要继续吗？" in capsys.readouterr().out


def test_ask_user_empty_answer(monkeypatch):
    """空白回答归一化为占位说明。"""
    monkeypatch.setattr("smithcode.renderer.read_user_input", lambda prompt="回答> ": "   ")
    assert ask_user([{"question": "问题"}]) == "（用户未输入内容）"


def test_ask_user_fail_closed_non_interactive(monkeypatch, capsys):
    """非交互 stdin 下不阻塞等待，返回已取消提示。"""
    monkeypatch.setattr("smithcode.tools.ask.confirmations_available", lambda: False)
    monkeypatch.setattr(
        "smithcode.renderer.read_user_input",
        lambda prompt="回答> ": pytest.fail("非交互不应读取输入"),
    )
    assert "无法向用户提问" in ask_user([{"question": "问题"}])


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
    assert perm.check("ask_user", {"questions": [{"question": "x"}]}) is False


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
    call = {"function": {"name": "ask_user", "arguments": json.dumps(
        {"questions": [{"question": "确认？"}]})}}
    asyncio.run(agent._execute_batch([call]))
    assert agent.session.messages[-1]["content"] == "继续"


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


# ---------- 复数入参：归一化与回传格式 ----------


def test_ask_user_passes_normalized_questions(monkeypatch):
    """ask_user 把 options 拍平成 labels + descriptions 对齐后交给 renderer.ask_form。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    seen = {}

    def fake_ask_form(questions):
        seen["questions"] = questions
        return ["A", "B"]

    monkeypatch.setattr(renderer.current(), "ask_form", fake_ask_form)
    out = ask_mod.ask_user([
        {"question": "Q1", "options": [{"label": "甲", "description": "说明"}, {"label": "乙"}]},
        {"question": "Q2", "multiple": True},
    ])
    assert seen["questions"] == [
        {"question": "Q1", "options": ["甲", "乙"], "descriptions": ["说明", ""], "multiple": False},
        {"question": "Q2", "options": [], "descriptions": [], "multiple": True},
    ]
    assert out == "1. Q1 → A\n2. Q2 → B"


def test_ask_user_clamps_options_to_five(monkeypatch):
    """强制遵守 1-5 个选项：模型多给时按上限截断。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    seen = {}

    def fake_ask_form(questions):
        seen["questions"] = questions
        return ["A"]

    monkeypatch.setattr(renderer.current(), "ask_form", fake_ask_form)
    ask_mod.ask_user([{
        "question": "选一个",
        "options": [{"label": f"O{i}"} for i in range(1, 9)],  # 8 项
    }])
    assert seen["questions"][0]["options"] == ["O1", "O2", "O3", "O4", "O5"]
    assert seen["questions"][0]["descriptions"] == ["", "", "", "", ""]


def test_ask_user_single_question_returns_answer(monkeypatch):
    """单题直接返回答案（与旧行为一致，不做编号包裹）。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    monkeypatch.setattr(renderer.current(), "ask_form", lambda questions: ["是的"])
    assert ask_mod.ask_user([{"question": "继续？"}]) == "是的"


def test_ask_user_multiple_marks_unanswered(monkeypatch):
    """多题中未答（取消）的项标记为「已取消」。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    monkeypatch.setattr(renderer.current(), "ask_form", lambda questions: ["A", ""])
    out = ask_mod.ask_user([{"question": "Q1"}, {"question": "Q2"}])
    assert out == "1. Q1 → A\n2. Q2 → （已取消）"


def test_ask_user_empty_questions_returns_error(monkeypatch):
    """空入参 / 缺题干的项不抛异常，返回可操作的报错文本。"""
    from smithcode.tools import ask as ask_mod

    assert ask_mod.ask_user([]).startswith("错误:")
    assert ask_mod.ask_user(None).startswith("错误:")
    assert ask_mod.ask_user([{"options": [{"label": "x"}]}]).startswith("错误:")


def test_ask_user_multiple_flag_passed_through(monkeypatch):
    """每题 multiple 标志透传到归一化结果。"""
    from smithcode import renderer
    from smithcode.tools import ask as ask_mod

    seen = {}

    def fake_ask_form(questions):
        seen["q"] = questions
        return ["A, B"]

    monkeypatch.setattr(renderer.current(), "ask_form", fake_ask_form)
    ask_mod.ask_user([{"question": "？", "options": [{"label": "A"}, {"label": "B"}], "multiple": True}])
    assert seen["q"][0]["multiple"] is True


def test_console_ask_form_loops_questions(monkeypatch, capsys):
    """CLI 默认实现逐题串行提问，多题带 (i/n) 前缀。"""
    from smithcode.renderer import ConsoleRenderer

    _inputs(monkeypatch, ["1", "要"])
    out = ConsoleRenderer().ask_form([
        {"question": "端口？", "options": ["本地", "远程"], "descriptions": ["", ""], "multiple": False},
        {"question": "鉴权？", "options": [], "descriptions": [], "multiple": False},
    ])
    assert out == ["本地", "要"]
    text = capsys.readouterr().out
    assert "（1/2）端口？" in text
    assert "（2/2）鉴权？" in text


def test_ask_user_describe_and_display():
    """工具块摘要：单题显示题干，多题显示「首题 等 N 项」；用 block 形态独立成块。"""
    from smithcode.tools.base import DESCRIBERS, DISPLAY

    describe = DESCRIBERS["ask_user"]
    assert describe({"questions": [{"question": "用哪个？"}]}) == "提问：用哪个？"
    assert describe({"questions": [{"question": "端口？"}, {"question": "鉴权？"}]}) == "提问：端口？ 等 2 项"
    assert describe({}) == "提问：?"
    assert DISPLAY["ask_user"] == "block"

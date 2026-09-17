"""会话标题的纯逻辑：请求构造、JSON 清洗与生成条件。"""

from smithcode import agent as agent_mod
from smithcode.sessions import build_title_request, clean_title, should_generate
from smithcode.sessions.title import _INTERNAL_PREFIXES


def test_clean_title_parses_json_and_fences():
    assert clean_title('{"title": "重构会话管理"}') == "重构会话管理"
    assert clean_title('```json\n{"title": "修复登录流程"}\n```') == "修复登录流程"
    assert clean_title('好的{"title": "数据库迁移"}') == "数据库迁移"


def test_clean_title_rejects_invalid_outputs():
    assert clean_title("") == ""
    assert clean_title("不是 JSON") == ""
    assert clean_title('{"title": ""}') == ""
    assert clean_title('{"title": "错误：无法生成标题"}') == ""
    # 超长句子不是标题
    assert clean_title('{"title": "' + "很长的标题" * 20 + '"}') == ""


def test_build_title_request_uses_first_user_and_assistant():
    messages = [
        {"role": "system", "content": "系统提示词"},
        {"role": "user", "content": "帮我重构 session.py"},
        {"role": "assistant", "content": "好的，我先看看"},
        {"role": "user", "content": "第二条消息"},
    ]
    request = build_title_request(messages)
    assert request[0]["role"] == "system"
    payload = request[1]["content"]
    assert "帮我重构 session.py" in payload
    assert "好的，我先看看" in payload
    assert "第二条消息" not in payload


def test_should_generate_rules():
    assert should_generate("", "") is True
    assert should_generate("已有标题", "") is False
    assert should_generate("自动标题", "auto") is False  # 只生成一次
    assert should_generate("用户标题", "user") is False  # 用户命名优先


def test_build_title_request_skips_interrupt_context():
    """中断回写是记账消息：首轮被中断后，标题仍按真实首轮取材、不被污染。"""
    messages = [
        {"role": "user", "content": "帮我重构 session.py"},
        {"role": "assistant", "content": "部分输出"},
        {"role": "user", "content": agent_mod.INTERRUPTED_CONTEXT},
        {"role": "user", "content": "继续"},
    ]
    payload = build_title_request(messages)[1]["content"]
    assert "帮我重构 session.py" in payload
    assert "部分输出" in payload
    assert "用户手动中断" not in payload
    assert "继续" not in payload  # 仍是首轮的标题，不是第二轮的


def test_build_title_request_leading_internal_note_does_not_shift_turn():
    """记账消息在前：轮次边界仍是真实用户轮，不把后面的轮次算进来。"""
    messages = [
        {"role": "user", "content": agent_mod.INTERRUPTED_CONTEXT},
        {"role": "user", "content": "首轮真实任务"},
        {"role": "assistant", "content": "首轮回复"},
        {"role": "user", "content": "第二轮任务"},
    ]
    payload = build_title_request(messages)[1]["content"]
    assert "首轮真实任务" in payload
    assert "首轮回复" in payload
    assert "第二轮任务" not in payload


def test_internal_prefixes_cover_all_bookkeeping_sources():
    """一致性守卫：新增记账消息必须同步 _INTERNAL_PREFIXES，否则标题悄悄被污染。

    逐条对照来源常量的真实前缀（改任一处文案都要同步另一处）。
    """
    from smithcode.skills import render as skills_render

    assert agent_mod.INTERRUPTED_CONTEXT.startswith(_INTERNAL_PREFIXES[0])
    assert agent_mod.STREAM_INTERRUPTED_CONTEXT.startswith(_INTERNAL_PREFIXES[1])
    assert skills_render.compacted_notice(["某技能"]).startswith(_INTERNAL_PREFIXES[2])
    assert skills_render.recall_notice(
        type("S", (), {"name": "某技能", "location": "x/SKILL.md", "base": "x"})()
    ).startswith(_INTERNAL_PREFIXES[3])
    assert agent_mod.MAX_ITERATIONS_WRAPUP.startswith(_INTERNAL_PREFIXES[4])

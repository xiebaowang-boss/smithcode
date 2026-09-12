"""会话标题的纯逻辑：请求构造、JSON 清洗与生成条件。"""

from smithcode.sessions import build_title_request, clean_title, should_generate


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

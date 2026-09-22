"""流式解析测试：`parse_stream` 的 chunk 累积语义，不依赖真实 API。"""

from types import SimpleNamespace

from smithcode.llm.stream import parse_stream, usage_to_dict


def _delta(content=None, reasoning=None, tool_calls=None):
    return SimpleNamespace(
        content=content, reasoning_content=reasoning, tool_calls=tool_calls)


def _chunk(delta=None, usage=None):
    return SimpleNamespace(
        usage=usage, choices=[] if delta is None else [SimpleNamespace(delta=delta)])


def _tc(index, call_id="", name="", arguments=""):
    return SimpleNamespace(
        index=index, id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments))


def test_content_and_reasoning_passthrough():
    """content / reasoning 增量实时透出，message 只交付归属（content 留空）。"""
    chunks = [
        _chunk(_delta(reasoning="先想")),
        _chunk(_delta(content="你好")),
        _chunk(_delta(content="世界")),
    ]
    events = list(parse_stream(chunks))
    assert events[0] == ("reasoning", "先想")
    assert [p for k, p in events if k == "content"] == ["你好", "世界"]
    messages = [p for k, p in events if k == "message"]
    assert messages == [{"role": "assistant", "content": ""}]
    assert not any(k == "usage" for k, _ in events)


def test_tool_calls_accumulate_by_index_out_of_order():
    """tool_calls 按 index 槽位累积：乱序到达、arguments 分片拼接。"""
    chunks = [
        _chunk(_delta(tool_calls=[_tc(1, call_id="c2", name="grep")])),
        _chunk(_delta(tool_calls=[_tc(0, call_id="c1", name="read")])),
        _chunk(_delta(tool_calls=[_tc(0, arguments='{"pa')])),
        _chunk(_delta(tool_calls=[_tc(1, arguments='{"pa'), _tc(0, arguments='th"}')])),
        _chunk(_delta(tool_calls=[_tc(1, arguments='th"}')])),
    ]
    messages = [p for k, p in parse_stream(chunks) if k == "message"]
    assert messages[0]["tool_calls"] == [
        {"id": "c1", "type": "function",
         "function": {"name": "read", "arguments": '{"path"}'}},
        {"id": "c2", "type": "function",
         "function": {"name": "grep", "arguments": '{"path"}'}},
    ]


def test_tool_calls_none_index_defaults_to_zero():
    """index 缺失视为 0，与后续同槽分片合并。"""
    chunks = [
        _chunk(_delta(tool_calls=[_tc(None, call_id="c", name="read")])),
        _chunk(_delta(tool_calls=[_tc(None, arguments="{}")])),
    ]
    messages = [p for k, p in parse_stream(chunks) if k == "message"]
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == "{}"


def test_usage_tail_packet_and_invalid_does_not_clobber():
    """纯 usage 尾包（无 choices）被捕获；解析失败的包不覆盖有效值。"""
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    chunks = [
        _chunk(_delta(content="好"), usage=usage),
        _chunk(usage="不是合法 usage"),  # usage_to_dict 失败 → 保留上一份
        _chunk(),  # 空 choices 纯 usage 包的另一种形态：直接跳过
    ]
    events = list(parse_stream(chunks))
    usages = [p for k, p in events if k == "usage"]
    assert usages == [usage]


def test_usage_to_dict_never_raises():
    """usage 结构异常一律返回 None，绝不影响对话流。"""
    assert usage_to_dict(None) is None
    assert usage_to_dict({"a": 1}) == {"a": 1}

    class _Bad:
        def model_dump(self):
            raise RuntimeError("坏的 usage")

    assert usage_to_dict(_Bad()) is None

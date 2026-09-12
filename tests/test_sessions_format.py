"""转录格式 v1：记录编解码、读取容错与崩溃修复（纯逻辑，不依赖 LLM）。"""

from smithcode.sessions import format


def test_record_roundtrip_via_line():
    record = format.msg_record({"role": "user", "content": "你好"})
    line = format.dump_record(record)
    assert line.endswith("\n")
    parsed = format.parse_line(line)
    assert parsed["v"] == format.FORMAT_VERSION
    assert parsed["t"] == "msg"
    assert parsed["m"]["content"] == "你好"


def test_parse_line_rejects_invalid_shapes():
    assert format.parse_line("") is None
    assert format.parse_line("不是 JSON") is None
    assert format.parse_line("[1, 2]") is None
    assert format.parse_line('{"v":1}') == {"v": 1}


def test_read_transcript_skips_bad_lines_and_partial_tail(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        format.dump_record(format.meta_record("sid", "cwd"))
        + "{坏的中间行}\n"
        + format.dump_record(format.msg_record({"role": "user", "content": "hi"}))
        + '{"v":1,"t":"msg","m":{"role":"assistant"',  # 崩溃残行（无换行结尾）
        encoding="utf-8",
    )
    records, bad = format.read_transcript(path)
    assert bad == 1  # 中间坏行计数；末行残行静默忽略
    assert [record["t"] for record in records] == ["meta", "msg"]


def test_repair_appends_placeholder_for_tail_dangling():
    messages = [
        {"role": "user", "content": "跑个任务"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1"}, {"id": "call_2"}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "结果1"},
    ]
    status, appended = format.repair_dangling_tool_calls(messages)
    assert status == "appended"
    assert [m["tool_call_id"] for m in appended] == ["call_2"]
    assert messages[-1]["content"] == format.CRASH_PLACEHOLDER
    # 修复后历史合法：再修一次无改动（幂等）
    assert format.repair_dangling_tool_calls(messages) == ("none", [])


def test_repair_truncates_mid_history_dangling():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "user", "content": "接着来"},  # 中段损坏：结果缺失但后续还有消息
    ]
    status, appended = format.repair_dangling_tool_calls(messages)
    assert status == "truncated"
    assert appended == []
    assert messages == []


def test_repair_ignores_satisfied_calls():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
    ]
    assert format.repair_dangling_tool_calls(messages) == ("none", [])

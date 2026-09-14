"""MCP 工具命名、schema 转换与结果映射测试。"""

from smithcode import config
from smithcode.mcp.catalog import build_spec, format_result, normalize_schema
from smithcode.mcp.secrets import Redactor


def test_exposed_name_sanitized_and_unique():
    taken = set()
    spec = build_spec("my server", {"name": "do/thing.v2"}, taken)
    assert spec.exposed == "mcp__my_server__do_thing_v2"

    other = build_spec("my server", {"name": "do/thing.v2"}, taken)
    assert other.exposed != spec.exposed
    assert other.exposed.startswith("mcp__my_server__do_thing_v2_")


def test_schema_prefix_normalization_and_defaults():
    spec = build_spec(
        "github",
        {
            "name": "search",
            "description": "搜索",
            "inputSchema": {"$schema": "x", "properties": {"q": {"type": "string"}}},
        },
        set(),
    )
    schema = spec.to_schema()
    assert schema["name"] == "mcp__github__search"
    assert schema["description"].startswith("[MCP:github]")
    assert schema["parameters"]["type"] == "object"
    assert "$schema" not in schema["parameters"]
    assert normalize_schema({}) == {"type": "object", "properties": {}}


def test_result_text_and_structured():
    text = format_result(
        {
            "content": [{"type": "text", "text": "hello"}],
            "structuredContent": {"ok": True},
        },
        Redactor(),
    )
    assert "hello" in text
    assert '"ok": true' in text


def test_result_is_error():
    text = format_result(
        {"content": [{"type": "text", "text": "boom"}], "isError": True}, Redactor()
    )
    assert text.startswith("错误: ")


def test_result_non_text_content():
    text = format_result(
        {
            "content": [
                {"type": "image", "data": "..."},
                {"type": "resource_link", "uri": "file://a"},
                {"type": "resource", "resource": {"uri": "file://b", "text": "res"}},
            ]
        },
        Redactor(),
    )
    assert "图片" in text
    assert "链接" in text
    assert "res" in text


def test_result_redacted_and_truncated(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_OUTPUT", 100)
    redactor = Redactor()
    redactor.add("super-secret-value")
    text = format_result(
        {"content": [{"type": "text", "text": "x super-secret-value " + "y" * 500}]},
        redactor,
    )
    assert "super-secret-value" not in text
    assert "省略中间" in text


def test_read_only_hint():
    spec = build_spec("s", {"name": "t", "annotations": {"readOnlyHint": True}}, set())
    assert spec.read_only is True
    assert build_spec("s", {"name": "t2"}, set()).read_only is False

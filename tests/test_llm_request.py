"""请求组装测试：`build_kwargs` 纯函数，不读全局配置、不依赖真实 API。"""

from smithcode.llm.request import ChatRequest, build_kwargs


def test_model_falls_back_to_default():
    kwargs = build_kwargs(ChatRequest(messages=[]), default_model="d")
    assert kwargs["model"] == "d"
    assert kwargs["stream"] is True
    assert kwargs["stream_options"] == {"include_usage": True}
    assert "tools" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert "extra_headers" not in kwargs


def test_model_override_and_tools_wrapped():
    schema = {"name": "read_file", "description": "读文件"}
    kwargs = build_kwargs(
        ChatRequest(messages=[{"role": "user", "content": "hi"}],
                    tools=[schema], model="m",
                    reasoning_effort="low",
                    extra_headers={"x-s": "abc"}),
        default_model="d",
    )
    assert kwargs["model"] == "m"
    assert kwargs["tools"] == [{"type": "function", "function": schema}]
    assert kwargs["reasoning_effort"] == "low"
    assert kwargs["extra_headers"] == {"x-s": "abc"}

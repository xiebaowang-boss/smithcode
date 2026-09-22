"""请求组装测试：`build_kwargs` 纯函数与 `TurnConfig` 轮级快照，不依赖真实 API。"""

from smithcode import config
from smithcode.llm.request import ChatRequest, TurnConfig, build_kwargs


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


def test_turn_capture_reads_config(monkeypatch):
    """轮级快照从全局配置 pin：模型直读，空 effort 回退默认档位。"""
    monkeypatch.setattr(config, "MODEL", "m-turn")
    monkeypatch.setattr(config, "REASONING_EFFORT", "low")
    turn = TurnConfig.capture()
    assert (turn.model, turn.effort) == ("m-turn", "low")

    monkeypatch.setattr(config, "REASONING_EFFORT", "")
    assert TurnConfig.capture().effort == config.DEFAULT_EFFORT


def test_turn_capture_model_override_wins(monkeypatch):
    """Agent 的 model 覆盖（`--model` / 构造参数）优先于全局配置。"""
    monkeypatch.setattr(config, "MODEL", "m-global")
    assert TurnConfig.capture("m-override").model == "m-override"


def test_turn_config_is_frozen():
    """快照不可变：轮内任何地方都改不动它，只能下一轮重新 pin。"""
    import dataclasses

    turn = TurnConfig(model="m", effort="low")
    try:
        turn.model = "other"
    except dataclasses.FrozenInstanceError:
        pass
    else:
        raise AssertionError("TurnConfig 应为 frozen")
    assert turn.model == "m"

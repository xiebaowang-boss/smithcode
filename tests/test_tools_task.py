"""task 工具测试：schema 注册、动态同步（类型 enum / 隐藏）与默认权限规则。"""
import dataclasses

import pytest

from smithcode import config, subagents
from smithcode.permission import evaluate
from smithcode.permission.engine import DEFAULT_RULES
from smithcode.tools import FUNCTIONS, HIDDEN, all_schemas
from smithcode.tools.task import sync_schema


@pytest.fixture(autouse=True)
def _cleanup():
    before = config.SUBAGENTS
    yield
    config.SUBAGENTS = before
    subagents.reset()
    sync_schema()


def _task_schema():
    return next(s for s in all_schemas() if s["name"] == "task")


def test_task_registered_with_builtin_types():
    schema = _task_schema()
    names = schema["parameters"]["properties"]["subagent_type"]["enum"]
    assert "explore" in names and "general" in names
    assert "task" in FUNCTIONS
    params = schema["parameters"]
    assert params["required"] == ["description", "prompt"]
    assert "pattern_arg" not in schema  # 内部键不进 LLM schema


def test_permission_pattern_is_subagent_type():
    from smithcode.tools import PATTERN_ARGS

    assert PATTERN_ARGS["task"] == "subagent_type"


def test_default_rule_allows_task_spawn():
    rule = evaluate(("task", "task"), "explore", DEFAULT_RULES)
    assert rule[2] == "allow"


def test_sync_schema_hides_task_when_disabled(monkeypatch):
    monkeypatch.setattr(
        config, "SUBAGENTS", dataclasses.replace(config.SUBAGENTS, enabled=False)
    )
    sync_schema()
    assert "task" in HIDDEN
    # 恢复默认后重新可见（同步动作本身是幂等的）
    monkeypatch.setattr(config, "SUBAGENTS", dataclasses.replace(config.SUBAGENTS, enabled=True))
    sync_schema()
    assert "task" not in HIDDEN


def test_sync_schema_reflects_custom_specs(monkeypatch):
    spec = subagents.SubAgentSpec(
        name="reviewer", description="代码审查", system_prompt="你是审查子代理。"
    )
    monkeypatch.setattr("smithcode.subagents.defs._specs", {"reviewer": spec})
    sync_schema()
    schema = _task_schema()
    assert schema["parameters"]["properties"]["subagent_type"]["enum"] == ["reviewer"]
    assert "代码审查" in schema["description"]


def test_describe_shows_type_and_label():
    from smithcode.tools import DESCRIBERS

    text = DESCRIBERS["task"]({"subagent_type": "explore", "description": "找认证"})
    assert text.startswith("task explore: 找认证")


def test_registered_stub_is_unreachable():
    """直接调用注册函数只返回兜底错误（真实执行走 Agent 预检特判）。"""
    assert "调度器" in FUNCTIONS["task"]()

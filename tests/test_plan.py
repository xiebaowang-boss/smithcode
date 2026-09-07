"""任务拆分与分步骤执行测试：plan 状态模块与 todo_write 工具。"""
import json

import pytest

from smithcode import config, plan
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.tools.todo import todo_write


@pytest.fixture(autouse=True)
def fresh_plan():
    """每测清空步骤清单，避免跨用例污染（plan 是会话级进程内状态）。"""
    plan.reset()
    yield
    plan.reset()


def test_todo_write_replaces_full_list():
    """todo_write 是整体替换而非增量：第二次提交只保留最新清单。"""
    todo_write(
        [
            {"content": "定位问题", "status": "in_progress"},
            {"content": "修复", "status": "pending"},
        ]
    )
    assert len(plan.current().items) == 2
    assert plan.current().items[0]["status"] == "in_progress"

    todo_write([{"content": "修复", "status": "completed"}])
    assert len(plan.current().items) == 1
    assert plan.current().items[0]["status"] == "completed"


def test_todo_write_cleans_invalid_input():
    """空内容忽略、非法状态降级为 pending、缺 status 默认 pending。"""
    result = todo_write(
        [
            {"content": "   ", "status": "in_progress"},
            {"content": "合理步骤", "status": "bogus"},
            {"content": "好步骤"},
        ]
    )
    assert len(plan.current().items) == 2
    assert plan.current().items[0]["status"] == "pending"
    assert "好步骤" in result


def test_todo_write_caps_list_size():
    big = [{"content": f"步骤{i}", "status": "pending"} for i in range(200)]
    todo_write(big)
    assert len(plan.current().items) == plan.MAX_ITEMS


def test_plan_render_shows_status_icons():
    todo_write(
        [
            {"content": "完成项", "status": "completed"},
            {"content": "进行中", "status": "in_progress"},
            {"content": "待办", "status": "pending"},
        ]
    )
    text = plan.render_current()
    assert "✓" in text and "●" in text and "○" in text
    assert "完成项" in text and "进行中" in text and "待办" in text


def test_plan_summary_counts():
    todo_write(
        [
            {"content": "a", "status": "completed"},
            {"content": "b", "status": "in_progress"},
            {"content": "c", "status": "pending"},
        ]
    )
    assert plan.summary() == "共 3 步 · 已完成 1 · 进行中 1"


def test_plan_summary_empty():
    assert plan.summary() == "暂无任务计划"
    assert "暂无任务计划" in plan.render_current()


def test_plan_render_reason_appended():
    todo_write([{"content": "修 bug", "status": "completed", "reason": "测试通过"}])
    assert "修 bug" in plan.render_current()
    assert "测试通过" in plan.render_current()


def test_plan_reset_clears():
    todo_write([{"content": "步骤"}])
    plan.reset()
    assert plan.render_current() != "步骤"


def test_system_prompt_instructs_todo_write():
    """系统提示词应包含任务拆分与分步骤执行规则。"""
    from smithcode.prompts import build_system_prompt

    assert "todo_write" in build_system_prompt()
    assert "in_progress" in build_system_prompt()


def _fake_llm_class(calls):
    """生成一个无参构造的假 LLM 类（Agent 里会 LLMClient() 实例化），按序回放消息。"""

    class FakeLLM:
        def __init__(self):
            self.calls = calls
            self.i = 0

        def chat_stream(self, messages, tools=None):
            if self.i < len(self.calls):
                msg = self.calls[self.i]
                self.i += 1
                yield ("message", msg)
            else:
                yield ("message", {"role": "assistant", "content": "完成"})

    return FakeLLM


def test_agent_executes_todo_write_and_renders(monkeypatch, capsys):
    """Agent 循环里 todo_write 正常执行、计划实时渲染、结果进入会话供模型可见。"""
    monkeypatch.setattr(
        "smithcode.agent.LLMClient",
        _fake_llm_class(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {
                                "name": "todo_write",
                                "arguments": json.dumps(
                                    {"todos": [{"content": "步骤一", "status": "in_progress"}]}
                                ),
                            },
                        }
                    ],
                },
            ]
        ),
    )
    agent = Agent(session=Session())
    assert agent.run("多步任务") == "完成"
    out = capsys.readouterr().out
    assert "[计划]" in out
    assert "步骤一" in out
    assert any("步骤一" in str(m.get("content", "")) for m in agent.session.messages)


def test_agent_todo_write_denied_by_user_rule(monkeypatch, tmp_path):
    """用户 deny 规则生效：todo_write 被拒时结果回传"用户拒绝了此操作"。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("smithcode.agent.LLMClient", _fake_llm_class([]))
    agent = Agent(session=Session())
    agent.permission.user_rules = [("todo_write", "*", "deny")]
    call = {
        "function": {
            "name": "todo_write",
            "arguments": json.dumps({"todos": [{"content": "x", "status": "pending"}]}),
        }
    }
    assert agent._execute(call)[0] == "用户拒绝了此操作"
    assert plan.current().items == []


def test_todo_write_allowed_by_default():
    from smithcode.permission import evaluate

    assert evaluate("todo_write", "*", [("todo_write", "*", "allow")])[2] == "allow"


def test_todo_write_can_be_denied():
    from smithcode.permission import Permission

    perm = Permission()
    perm.user_rules = [("todo_write", "*", "deny")]
    assert perm.check("todo_write", {"todos": [{"content": "x"}]}) is False
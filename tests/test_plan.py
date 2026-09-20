"""任务拆分与分步骤执行测试：plan 状态模块与 todo_write 工具。"""
import asyncio
import json

import pytest

from smithcode import config, frontend, plan
from smithcode.agent import Agent
from smithcode.frontend.console import ConsoleFrontend
from smithcode.session import Session
from smithcode.tools.todo import todo_read, todo_write


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
            {"title": "定位问题", "status": "in_progress"},
            {"title": "修复", "status": "pending"},
        ]
    )
    assert len(plan.current().items) == 2
    assert plan.current().items[0]["status"] == "in_progress"

    todo_write([{"title": "修复", "status": "completed"}])
    assert len(plan.current().items) == 1
    assert plan.current().items[0]["status"] == "completed"


def test_todo_write_cleans_invalid_input():
    """空内容忽略、非法状态降级为 pending、缺 status 默认 pending。"""
    result = todo_write(
        [
            {"title": "   ", "status": "in_progress"},
            {"title": "合理步骤", "status": "bogus"},
            {"title": "好步骤"},
        ]
    )
    assert len(plan.current().items) == 2
    assert plan.current().items[0]["status"] == "pending"
    assert "好步骤" in result


def test_todo_write_caps_list_size():
    big = [{"title": f"步骤{i}", "status": "pending"} for i in range(200)]
    todo_write(big)
    assert len(plan.current().items) == plan.MAX_ITEMS


def test_plan_render_shows_status_icons():
    todo_write(
        [
            {"title": "完成项", "status": "completed"},
            {"title": "进行中", "status": "in_progress"},
            {"title": "待办", "status": "pending"},
        ]
    )
    text = plan.render_current()
    assert "✓" in text and "●" in text and "○" in text
    assert "完成项" in text and "进行中" in text and "待办" in text


def test_plan_summary_counts():
    todo_write(
        [
            {"title": "a", "status": "completed"},
            {"title": "b", "status": "in_progress"},
            {"title": "c", "status": "pending"},
        ]
    )
    assert plan.summary() == "共 3 步 · 已完成 1 · 进行中 1"


def test_plan_summary_empty():
    assert plan.summary() == "暂无任务计划"
    assert "暂无任务计划" in plan.render_current()


def test_plan_render_reason_appended():
    todo_write([{"title": "修 bug", "status": "completed", "reason": "测试通过"}])
    assert "修 bug" in plan.render_current()
    assert "测试通过" in plan.render_current()


def test_plan_reset_clears():
    todo_write([{"title": "步骤"}])
    plan.reset()
    assert plan.render_current() != "步骤"


def test_todo_write_assigns_ids_and_keeps_title_immutable():
    """服务端分配 id：带 id 更新时标题不可变，描述/状态可改。"""
    todo_write(
        [
            {"title": "定位", "status": "pending"},
            {"title": "修复", "status": "pending"},
        ]
    )
    first, second = plan.current().items
    assert first["id"] and second["id"]

    todo_write(
        [
            {"id": first["id"], "title": "改成别的", "status": "in_progress", "description": "详情"},
            {"id": second["id"], "title": "修复", "status": "completed"},
        ]
    )
    items = plan.current().items
    assert items[0]["title"] == "定位"  # 标题没被改掉
    assert items[0]["status"] == "in_progress"
    assert items[0]["description"] == "详情"
    assert items[1]["title"] == "修复"


def test_todo_write_title_match_without_id_preserves_identity():
    """无 id 但标题匹配 → 视为同一项（保住 id 与标题），新标题才是新项。"""
    todo_write([{"title": "甲", "status": "pending"}])
    old_id = plan.current().items[0]["id"]
    todo_write(
        [
            {"title": "甲", "status": "completed"},
            {"title": "乙", "status": "pending"},
        ]
    )
    items = plan.current().items
    assert len(items) == 2
    assert items[0]["id"] == old_id
    assert items[0]["status"] == "completed"
    assert items[1]["id"] != old_id


def test_todo_write_description_rendered():
    todo_write([{"title": "重构", "status": "in_progress", "description": "把旧 API 换成新 API"}])
    text = plan.render_current()
    assert "重构" in text
    assert "把旧 API 换成新 API" in text


def test_render_titles_omits_description_and_reason():
    todo_write(
        [{"title": "重构", "status": "in_progress", "description": "细节", "reason": "计划调整"}]
    )
    titles = plan.render_titles()
    assert "重构" in titles
    assert "细节" not in titles
    assert "计划调整" not in titles


def test_todo_read_returns_snapshot_and_filters():
    todo_write(
        [
            {"title": "a", "status": "completed"},
            {"title": "b", "status": "in_progress"},
            {"title": "c", "status": "pending"},
        ]
    )
    assert "b" in todo_read()
    pending = todo_read(status="pending")
    assert "c" in pending and "a" not in pending and "b" not in pending
    assert todo_read(summary_only=True) == "共 3 步 · 已完成 1 · 进行中 1"


def test_todo_read_empty():
    assert "暂无任务计划" in todo_read()


def test_todo_read_describe_works():
    """todo_read 的终端短摘要能正常生成（describe 只在展示时调用，别回归成 TodoList.summary）。"""
    todo_write([{"title": "步骤", "status": "pending"}])
    from smithcode.agent import Agent

    agent = Agent(session=Session())
    assert agent._describe("todo_read", {}) == "plan (共 1 步)"


def test_system_prompt_instructs_todo_write():
    """系统提示词应包含任务拆分与分步骤执行规则。"""
    from smithcode.llm.prompts import build_system_prompt

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
                                    {"todos": [{"title": "步骤一", "status": "in_progress"}]}
                                ),
                            },
                        }
                    ],
                },
            ]
        ),
    )
    agent = Agent(session=Session())
    frontend.attach(agent.events, ConsoleFrontend())
    assert asyncio.run(agent.run("多步任务")).text == "完成"
    out = capsys.readouterr().out
    assert "[计划]" in out
    assert "步骤一" in out
    assert any("步骤一" in str(m.get("content", "")) for m in agent.session.messages)


def _todo_write_call(call_id, todos):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "todo_write",
                    "arguments": json.dumps({"todos": todos}),
                },
            }
        ],
    }


class _PlanSubscriber:
    """事件订阅者：记录工具行的出现与 plan 更新（前端就是这么消费的）。

    工具行的可见性由 `ToolEnd.expand` + 事件本身决定：新建清单发一条
    `PlanUpdate(created=True)`，后续更新只发 `created=False` 的那条。
    """

    def __init__(self):
        self.tool_calls = []
        self.plans = []

    def __call__(self, env):
        from smithcode.event.catalog import PlanUpdate, ToolStart

        if isinstance(env.data, ToolStart):
            self.tool_calls.append((env.data.name, env.data.display))
        elif isinstance(env.data, PlanUpdate):
            self.plans.append((env.data.created, env.data.tool_call_id))


def test_agent_prints_plan_only_when_created(monkeypatch, capsys):
    """新建清单打印一次 [计划]；后续每步更新只静默刷新，不再往对话区重复打印。"""
    monkeypatch.setattr(
        "smithcode.agent.LLMClient",
        _fake_llm_class(
            [
                _todo_write_call(
                    "1",
                    [
                        {"title": "步骤一", "status": "in_progress"},
                        {"title": "步骤二", "status": "pending"},
                    ],
                ),
                _todo_write_call(
                    "2",
                    [
                        {"title": "步骤一", "status": "completed"},
                        {"title": "步骤二", "status": "in_progress"},
                    ],
                ),
            ]
        ),
    )
    agent = Agent(session=Session())
    frontend.attach(agent.events, ConsoleFrontend())
    assert asyncio.run(agent.run("多步任务")).text == "完成"
    out = capsys.readouterr().out
    assert out.count("[计划]") == 1
    assert out.count("步骤一") == 1


def test_agent_skips_tool_row_on_plan_update(monkeypatch):
    """新建清单生成可折叠的 plan 工具块；后续更新不再生成工具行（避免刷屏）。"""
    monkeypatch.setattr(
        "smithcode.agent.LLMClient",
        _fake_llm_class(
            [
                _todo_write_call("1", [{"title": "步骤一", "status": "in_progress"}]),
                _todo_write_call("2", [{"title": "步骤一", "status": "completed"}]),
            ]
        ),
    )
    cap = _PlanSubscriber()
    agent = Agent(session=Session())
    agent.events.subscribe(cap)

    asyncio.run(agent.run("多步任务"))

    todo_rows = [display for name, display in cap.tool_calls if name == "todo_write"]
    assert todo_rows == ["block"]  # 仅新建时上屏，且为可折叠详情块
    assert [created for created, _ in cap.plans] == [True, False]


def test_agent_todo_write_denied_by_user_rule(monkeypatch, tmp_path):
    """用户 deny 规则生效：todo_write 被拒时结果回传"用户拒绝了此操作"。"""
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("smithcode.agent.LLMClient", _fake_llm_class([]))
    agent = Agent(session=Session())
    agent.permission.user_rules = [("todo_write", "*", "deny")]
    call = {
        "function": {
            "name": "todo_write",
            "arguments": json.dumps({"todos": [{"title": "x", "status": "pending"}]}),
        }
    }
    asyncio.run(agent._execute_batch([call]))
    assert agent.session.messages[-1]["content"] == "用户拒绝了此操作"
    assert plan.current().items == []


def test_todo_write_allowed_by_default():
    from smithcode.permission import evaluate

    assert evaluate("todo_write", "*", [("todo_write", "*", "allow")])[2] == "allow"


def test_todo_write_can_be_denied():
    from smithcode.permission import Permission

    perm = Permission()
    perm.user_rules = [("todo_write", "*", "deny")]
    assert perm.check("todo_write", {"todos": [{"title": "x"}]}) is False
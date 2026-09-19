"""会话持有 goal / plan / skills 的**状态实例**（第 8 项）。

改动前这三个模块各自是进程级单例（`goal._current` / `plan._current` /
`skills.state._skills`），恢复或切换会话时会串味。现在状态实例由 `AgentSession`
持有，模块级函数通过 `bind()` 指向当前会话的实例（既有读取点因此不必逐个改写）。
"""

from __future__ import annotations

from smithcode import goal, plan
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.skills import state as skills_state


def _agent(monkeypatch) -> Agent:
    class FakeLLM:
        def chat_stream(self, messages, tools=None):
            yield ("message", {"role": "assistant", "content": "好"})

    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: FakeLLM())
    return Agent(session=Session())


def test_session_owns_state_instances(monkeypatch):
    session = _agent(monkeypatch).session_owner

    assert isinstance(session.goal_state, goal.GoalState)
    assert isinstance(session.plan_state, plan.PlanState)
    assert isinstance(session.skills_state, skills_state.SkillsState)
    # 模块函数此刻指向本会话的实例（既有读取点因此自动正确）
    assert goal.active_state() is session.goal_state
    assert plan.active_state() is session.plan_state
    assert skills_state.active_state() is session.skills_state


def test_state_parts_are_bound_to_the_session_instance(monkeypatch):
    """注册表里的投影读写必须落在会话实例上，而不是"当时恰好绑定的实例"。"""
    agent = _agent(monkeypatch)
    session = agent.session_owner
    session.goal_state.current = None
    goal.set("会话一的目标")

    parts = {part.name: part for part in agent._state_registry()}
    # 换绑到别处：注册表仍然读写会话实例（显式绑定，不依赖活动实例）
    other = goal.GoalState()
    goal.bind(other)
    try:
        assert parts["goal"].snapshot()["objective"] == "会话一的目标"
        parts["goal"].reset()
        assert session.goal_state.current is None
        assert other.current is None  # 没有误伤别的实例
    finally:
        goal.bind(session.goal_state)


def test_two_sessions_do_not_share_goal_state(monkeypatch):
    """两个会话各有自己的目标：切换后互不影响（单例时代做不到）。"""
    first = _agent(monkeypatch).session_owner
    goal.set("第一个目标")
    second = _agent(monkeypatch).session_owner  # 建立时继承当前活动状态
    assert second.goal_state.current.objective == "第一个目标"

    goal.clear()  # 作用在第二个会话上
    second.goal_state.current = None
    first.bind_state()  # 切回第一个会话

    assert goal.current().objective == "第一个目标"
    assert second.goal_state.current is None


def test_plan_and_skills_state_are_per_session(monkeypatch):
    first = _agent(monkeypatch).session_owner
    plan.restore({"items": [{"id": "1", "title": "步骤", "status": "pending"}]})
    skills_state.bind(first.skills_state)
    skills_state.restore([])

    second = _agent(monkeypatch).session_owner  # 继承

    assert [item["title"] for item in second.plan_state.snapshot()["items"]] == ["步骤"]
    # 会话内清空不影响另一个会话的实例
    second.plan_state.reset()
    assert second.plan_state.snapshot()["items"] == []
    assert first.plan_state.snapshot()["items"] != []


def test_state_module_functions_still_work_without_a_session():
    """没有会话时回到进程级默认实例（直接调模块函数的测试、无 Agent 的路径）。"""
    goal.bind(None)
    plan.bind(None)
    skills_state.bind(None)

    assert goal.active_state() is goal.default_state()
    goal.set("无会话的目标")
    assert goal.current().objective == "无会话的目标"
    goal.bind(None)
    assert goal.current().objective == "无会话的目标"  # 默认实例保留
    goal.reset()

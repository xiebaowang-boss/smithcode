"""goal_update / goal_read 工具测试：状态声明、阻碍门槛与默认权限。"""

import pytest

from smithcode import goal
from smithcode.permission import DEFAULT_RULES, evaluate
from smithcode.tools import FUNCTIONS


@pytest.fixture(autouse=True)
def fresh_goal():
    goal.reset()
    yield
    goal.reset()


def test_goal_update_without_goal_errors():
    text = FUNCTIONS["goal_update"](status="complete", summary="证据")
    assert "没有持久目标" in text


def test_goal_update_complete_marks_and_reports():
    goal.set("目标", max_turns=5)
    goal.begin_turn()
    goal.note_run(("read_file",), total_tokens=500)
    text = FUNCTIONS["goal_update"](status="complete", summary="pytest 全部通过")
    current = goal.current()
    assert current.status == goal.COMPLETE
    assert current.evidence == "pytest 全部通过"
    assert "标记为完成" in text and "1/5" in text


def test_goal_update_blocked_needs_three_turns():
    goal.set("目标")
    for _ in range(2):
        goal.begin_turn()
        text = FUNCTIONS["goal_update"](status="blocked", summary="缺少 API key")
        assert "已记录" in text
        assert goal.current().status == goal.ACTIVE
    goal.begin_turn()
    text = FUNCTIONS["goal_update"](status="blocked", summary="缺少 API key")
    assert goal.current().status == goal.BLOCKED
    assert "受阻" in text


def test_goal_update_invalid_status():
    goal.set("目标")
    text = FUNCTIONS["goal_update"](status="pause", summary="x")
    assert "未知状态" in text


def test_goal_read_without_goal():
    assert "没有持久目标" in FUNCTIONS["goal_read"]()


def test_goal_read_snapshot_contains_state():
    goal.set("迁移模块", max_turns=8)
    goal.begin_turn()
    text = FUNCTIONS["goal_read"]()
    assert "迁移模块" in text
    assert "进行中" in text and "1/8" in text


def test_goal_tools_allowed_by_default():
    assert evaluate("goal_update", "*", DEFAULT_RULES)[2] == "allow"
    assert evaluate("goal_read", "*", DEFAULT_RULES)[2] == "allow"

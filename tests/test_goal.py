"""持久目标状态机测试：生命周期、阻碍审计、预算、提示词与系统提示词段。"""

import pytest

from smithcode import config, goal


@pytest.fixture(autouse=True)
def fresh_goal():
    """每个用例前后清空目标，防止跨用例污染。"""
    goal.reset()
    yield
    goal.reset()


# ---------- 生命周期 ----------


def test_set_and_status():
    g = goal.set("修复测试", max_turns=5)
    assert goal.is_set() and goal.is_active()
    assert g.objective == "修复测试"
    assert g.max_turns == 5
    assert goal.marker() == "◎ 目标 0/5"
    assert "修复测试" in goal.render_status()


def test_set_default_budget_from_config(monkeypatch):
    monkeypatch.setattr(config, "GOAL_MAX_TURNS", 7)
    assert goal.set("x").max_turns == 7


def test_set_replaces_previous_goal():
    goal.set("旧目标", max_turns=3)
    g = goal.set("新目标", max_turns=9)
    assert g.objective == "新目标" and g.max_turns == 9
    assert g.turns == 0


def test_begin_turn_and_token_accounting():
    goal.set("x", max_turns=3, tokens_at_start=100)
    goal.begin_turn()
    goal.begin_turn()
    goal.note_run(("read_file",), total_tokens=250)
    g = goal.current()
    assert g.turns == 2
    assert g.tokens_used == 150
    assert g.turns_left == 1


def test_pause_resume_clear():
    goal.set("x")
    assert goal.pause("用户暂停")
    assert goal.current().status == goal.PAUSED
    assert not goal.is_active()
    assert "已暂停" in goal.render_status()

    assert goal.resume()
    assert goal.is_active()

    assert goal.clear()
    assert not goal.is_set()
    assert goal.render_status() == "当前没有持久目标。"


def test_complete_sets_evidence_and_end():
    goal.set("x")
    goal.complete("pytest 全部通过")
    g = goal.current()
    assert g.status == goal.COMPLETE
    assert g.evidence == "pytest 全部通过"
    assert g.ended_at is not None
    assert "已完成" in goal.render_status()


def test_budget_limited_note():
    goal.set("x", max_turns=2)
    assert goal.budget_limited()
    g = goal.current()
    assert g.status == goal.BUDGET_LIMITED
    assert "2" in g.note
    assert not goal.budget_limited()  # 非 active 不重复标记


def test_resume_after_budget_resets_turn_window():
    goal.set("x", max_turns=2)
    goal.begin_turn()
    goal.begin_turn()
    goal.budget_limited()
    assert goal.resume()
    assert goal.current().turns == 0  # 用户明确继续：开启新预算窗口
    assert goal.is_active()


def test_pause_only_from_active():
    goal.set("x")
    goal.complete("done")
    assert not goal.pause("迟到的暂停")


# ---------- 阻碍审计（同一阻碍连续 3 个回合才接受） ----------


def test_blocked_requires_three_turns():
    goal.set("x")
    for expected in (1, 2):
        goal.begin_turn()
        accepted, message = goal.try_block("缺少 API key")
        assert not accepted
        assert goal.current().status == goal.ACTIVE
        assert f"{expected}/{goal.BLOCKED_THRESHOLD}" in message
    goal.begin_turn()
    accepted, message = goal.try_block("缺少 API key")
    assert accepted
    assert goal.current().status == goal.BLOCKED
    assert goal.current().evidence == "缺少 API key"
    assert "等待用户指示" in message


def test_blocked_same_turn_not_double_counted():
    goal.set("x")
    goal.begin_turn()
    for _ in range(3):
        accepted, _ = goal.try_block("同一阻碍")
        assert not accepted
    assert goal.current().status == goal.ACTIVE
    assert goal.current().blocked_streak == 1


def test_blocked_requires_reason():
    goal.set("x")
    goal.begin_turn()
    accepted, message = goal.try_block("   ")
    assert not accepted
    assert "summary" in message


def test_changed_blocked_reason_restarts_streak():
    goal.set("x")
    goal.begin_turn()
    goal.try_block("阻碍 A")
    goal.begin_turn()
    goal.try_block("阻碍 A")
    goal.begin_turn()
    accepted, _ = goal.try_block("阻碍 B")
    assert not accepted
    assert goal.current().blocked_streak == 1


def test_progress_tool_resets_blocked_streak():
    goal.set("x")
    goal.begin_turn()
    goal.try_block("阻碍 A")
    goal.note_run(("read_file",))  # 有推进动作
    assert goal.current().blocked_streak == 0
    goal.begin_turn()
    accepted, _ = goal.try_block("阻碍 A")
    assert not accepted  # 连击已重置


def test_goal_tools_do_not_reset_blocked_streak():
    goal.set("x")
    goal.begin_turn()
    goal.try_block("阻碍 A")
    goal.note_run(("goal_read", "goal_update"))  # 目标工具不算推进
    assert goal.current().blocked_streak == 1


# ---------- 提示词 ----------


def test_start_prompt_contains_objective_and_audit():
    goal.set("让 pytest 全绿", max_turns=20)
    text = goal.current().start_prompt()
    assert "让 pytest 全绿" in text
    assert "goal_update" in text
    assert "完成审计" in text
    assert "20" in text


def test_continuation_prompt_contains_budget_and_audit():
    goal.set("目标", max_turns=10)
    goal.begin_turn()
    text = goal.current().continuation_prompt()
    assert "第 1/10 回合" in text
    assert "剩余 9 回合" in text
    assert "完成审计" in text
    assert "goal_update" in text


def test_wrapup_prompt_stops_new_work():
    goal.set("目标", max_turns=3)
    text = goal.current().wrapup_prompt()
    assert "预算" in text
    assert "收尾" in text
    assert "不要开始新的实质工作" in text


# ---------- 系统提示词注入段 ----------


def test_render_section_empty_without_goal():
    assert goal.render_section() == ""
    assert goal.marker() == ""


def test_render_section_stable_while_active():
    """回合数与 token 变化不改变动态段——保护系统提示词前缀缓存。"""
    goal.set("目标 A", max_turns=5)
    first = goal.render_section()
    assert "目标 A" in first and "进行中" in first
    goal.begin_turn()
    goal.note_run(("read_file",), total_tokens=9999)
    assert goal.render_section() == first


def test_render_section_reflects_status_change():
    goal.set("目标 A")
    goal.complete("证据 X")
    section = goal.render_section()
    assert "已完成" in section and "证据 X" in section
    assert "当前持久目标" in section


def test_reset_clears_goal():
    goal.set("x")
    goal.reset()
    assert not goal.is_set() and goal.marker() == ""


# ---------- 侧边栏卡片内容 ----------


def test_sidebar_snapshot_tracks_progress_and_lifecycle():
    goal.set("迁移模块", max_turns=5)
    goal.begin_turn()
    goal.note_run(("read_file",), total_tokens=1234)
    title, body = goal.sidebar()
    assert title == "目标 · 1/5"
    assert "迁移模块" in body
    assert "1,234" in body and "进行中" in body

    goal.complete("pytest 全部通过")
    title, body = goal.sidebar()
    assert title == "目标 · 已完成"
    assert "证据" in body and "pytest 全部通过" in body

    goal.reset()
    assert goal.sidebar() is None

"""斜杠命令框架测试：注册表发现、dispatch 分发、参数/异常兜底与各命令语义。"""

from types import SimpleNamespace

import pytest

from smithcode import commands, config, goal
from smithcode.commands import base
from smithcode.commands.base import CommandResult
from smithcode.llm.models import DEFAULT_EFFORTS


@pytest.fixture(autouse=True)
def _fresh_goal():
    """命令用例共享全局目标单例，前后清空防止跨用例污染。"""
    goal.reset()
    yield
    goal.reset()


@pytest.fixture(autouse=True)
def _fresh_skills():
    """技能是进程级单例：命令用例前后清空，防止补全/兜底读到其他用例的技能。"""
    from smithcode import skills

    skills.clear()
    yield
    skills.clear()


class _StubAgent:
    """最小 Agent 桩：只带命令会用到的属性与方法。"""

    def __init__(self):
        self.saved_path = "/tmp/session.json"
        self.reset_called = False
        self.session = SimpleNamespace(
            reset=self._reset,
            save=lambda: self.saved_path,
            messages=[],
            usage=SimpleNamespace(summary=lambda: "用量摘要"),
        )
        self.permission = SimpleNamespace(session_rules=["旧规则"])
        self.context = SimpleNamespace(compact_count=0, last_actual=None)
        self.models = SimpleNamespace(list=lambda: ["a", "b", "c"])

    def _reset(self):
        self.reset_called = True

    def new_session(self):
        self.reset_called = True


def _run(text, **kwargs):
    agent = _StubAgent()
    return agent, commands.dispatch(agent, text, **kwargs)


# ---------- 注册表：内置命令齐全，all_commands 排序去重 ----------

def test_all_builtin_commands_registered():
    names = {cmd.name for cmd in commands.all_commands()}
    assert {"exit", "new", "save", "compact", "help", "plan", "usage", "context", "model", "effort", "goal"} <= names


def test_all_commands_sorted_and_deduped():
    cmds = commands.all_commands()
    names = [cmd.name for cmd in cmds]
    assert names == sorted(set(names))


def test_register_with_alias_points_to_same_command():
    base.register("faketest", "测试别名", aliases=("ft",))(
        lambda ctx: CommandResult()
    )
    try:
        assert commands.get_command("ft") is commands.get_command("faketest")
        assert [c.name for c in commands.all_commands()].count("faketest") == 1
    finally:
        base.COMMANDS.pop("faketest", None)
        base.COMMANDS.pop("ft", None)


def test_register_immediate_flag():
    """immediate 通过注册透传；默认 False。"""
    base.register("immtest", "测试立即执行", immediate=True)(lambda ctx: CommandResult())
    try:
        assert base.get_command("immtest").immediate is True
        assert base.get_command("help").immediate is False
    finally:
        base.COMMANDS.pop("immtest", None)


def test_model_command_is_immediate():
    assert base.get_command("model").immediate is True


def test_sessions_command_is_immediate():
    """/sessions 与 /model、/skills 一致：TUI 菜单选中即弹选择框。"""
    assert base.get_command("sessions").immediate is True


# ---------- /effort：思考强度切换（本地档位列表，交互同 /model） ----------

def test_effort_builtin_default_is_high():
    """内置默认档位为 high，且在候选列表中。"""
    assert config.DEFAULT_EFFORT == "high"
    assert config.DEFAULT_EFFORT in DEFAULT_EFFORTS


def test_effort_switches_with_arg(monkeypatch):
    monkeypatch.setattr(config, "REASONING_EFFORT", "low")
    _, outcome = _run("/effort high")
    assert config.REASONING_EFFORT == "high"
    assert outcome.text is None  # 静默切换：反馈由底栏状态刷新承担
    assert outcome.refresh_status


def test_effort_without_arg_returns_select(monkeypatch):
    monkeypatch.setattr(config, "REASONING_EFFORT", "medium")
    _, outcome = _run("/effort")
    assert outcome.select is not None
    assert outcome.select.command == "effort"
    values = [c.value for c in outcome.select.items]
    assert values == list(DEFAULT_EFFORTS)
    currents = [c.current for c in outcome.select.items]
    assert currents[values.index("medium")] is True
    assert currents.count(True) == 1


def test_effort_default_current_when_unset(monkeypatch):
    """REASONING_EFFORT 为空时按内置默认档位（config.DEFAULT_EFFORT）标记当前项。"""
    monkeypatch.setattr(config, "REASONING_EFFORT", None)
    _, outcome = _run("/effort")
    values = [c.value for c in outcome.select.items]
    assert outcome.select.items[values.index(config.DEFAULT_EFFORT)].current is True


def test_effort_command_is_immediate():
    assert base.get_command("effort").immediate is True


# ---------- dispatch：分发、未知命令、参数校验、异常兜底 ----------

def test_dispatch_unknown_command():
    _, outcome = _run("/nope")
    assert "未知命令" in outcome.text
    assert outcome.style == "red"
    assert not outcome.exit


def test_dispatch_rejects_args_on_no_arg_command():
    agent, outcome = _run("/compact 多余参数")
    assert "用法" in outcome.text
    assert not agent.reset_called  # 未执行命令本体


def test_dispatch_accepts_args_flag():
    base.register("argtest", "测试参数", usage="/argtest <名字>", accepts_args=True)(
        lambda ctx: CommandResult(text=f"收到 {ctx.args[0]}")
    )
    try:
        _, outcome = _run("/argtest hello")
        assert "收到 hello" in outcome.text
    finally:
        base.COMMANDS.pop("argtest", None)


def test_dispatch_catches_handler_exception():
    def boom(ctx):
        raise ValueError("炸了")

    base.register("boomtest", "测试异常")(boom)
    try:
        _, outcome = _run("/boomtest")
        assert "[命令出错]" in outcome.text
        assert "ValueError" in outcome.text
    finally:
        base.COMMANDS.pop("boomtest", None)


def test_dispatch_empty_name_falls_to_unknown():
    _, outcome = _run("/")
    assert "未知命令" in outcome.text


# ---------- complete_commands：补全菜单的前缀过滤 ----------

def test_complete_commands_filters_by_prefix():
    all_names = sorted({c.name for c in commands.all_commands()})
    assert [c.name for c in base.complete_commands("")] == all_names
    assert [c.name for c in base.complete_commands("he")] == ["help"]
    assert [c.name for c in base.complete_commands("sa")] == ["save"]
    assert base.complete_commands("zzz") == []


def test_complete_commands_excludes_aliases():
    base.register("comptesta", "测试补全", aliases=("ct",))(lambda ctx: CommandResult())
    try:
        names = [c.name for c in base.complete_commands("comptesta")]
        assert names == ["comptesta"]  # 别名不作为候选出现
    finally:
        base.COMMANDS.pop("comptesta", None)
        base.COMMANDS.pop("ct", None)


# ---------- 各命令语义与结果标记 ----------

def test_exit_requests_quit_without_text():
    _, outcome = _run("/exit")
    assert outcome.exit
    assert outcome.text is None


def test_new_delegates_to_agent_and_sets_flags():
    """/new 只委托 Agent.new_session（重置语义在 test_agent 验证），命令层管反馈与标记。"""
    agent, outcome = _run("/new")
    assert agent.reset_called
    assert "已开启新会话" in outcome.text  # 仅 REPL 展示，TUI 不渲染该文本
    assert outcome.session_reset and outcome.refresh_status


def test_save_reports_path():
    agent, outcome = _run("/save")
    assert agent.saved_path in outcome.text
    assert "会话已保存" in outcome.text


def test_compact_returns_host_intent():
    """/compact 只声明意图（由宿主后台执行），不在命令层同步发起请求。"""
    _, outcome = _run("/compact")
    assert outcome.start_compact is True
    assert outcome.text is None  # 进度与结果文案由宿主按运行状态给出


def test_compact_report_maps_status():
    assert commands.compact_report("ok") == ("上下文压缩完成。", "green")
    assert "已取消" in commands.compact_report("cancelled")[0]
    assert "没有可压缩的上下文" in commands.compact_report("empty")[0]
    assert "正在压缩" in commands.COMPACT_RUNNING


def test_usage_and_context_are_blocks_with_status_refresh():
    _, outcome = _run("/usage")
    assert outcome.kind == "block"
    assert "用量摘要" in outcome.text
    assert outcome.refresh_status

    _, outcome2 = _run("/context")
    assert outcome2.kind == "block"
    assert outcome2.text  # 空会话也能出报告
    assert outcome2.refresh_status


def test_plan_block_includes_summary_header():
    _, outcome = _run("/plan")
    assert outcome.kind == "block"
    assert outcome.text.startswith("[计划]")


def test_help_lists_commands_and_footer():
    _, outcome = _run("/help")
    assert outcome.kind == "block"
    for name in ("exit", "new", "save", "compact", "plan", "usage", "context"):
        assert f"/{name}" in outcome.text
    assert base.HELP_FOOTER in outcome.text


def test_help_text_alignment_covers_all_commands():
    text = base.help_text()
    for cmd in commands.all_commands():
        assert (cmd.usage or "/" + cmd.name) in text
        assert cmd.description in text


# ---------- /model：带参直接切换，无参返回选择意图 ----------

def test_model_switches_with_arg(monkeypatch):
    monkeypatch.setattr(config, "MODEL", "old-model")
    _, outcome = _run("/model new-model")
    assert config.MODEL == "new-model"
    assert outcome.text is None  # 静默切换：反馈由底栏状态刷新承担
    assert outcome.refresh_status
    assert outcome.select is None


def test_model_without_arg_returns_select(monkeypatch):
    monkeypatch.setattr(config, "MODEL", "b")
    _, outcome = _run("/model")
    assert outcome.select is not None
    assert outcome.select.command == "model"
    assert [c.value for c in outcome.select.items] == ["a", "b", "c"]
    assert [c.current for c in outcome.select.items] == [False, True, False]


def test_model_without_candidates_hints(monkeypatch):
    monkeypatch.setattr(config, "MODEL", "only")
    agent = _StubAgent()
    agent.models = SimpleNamespace(list=lambda: ["only"])
    outcome = commands.dispatch(agent, "/model")
    assert outcome.select is None
    assert "没有可切换的候选模型" in outcome.text


# ---------- /goal：持久目标设定、状态与生命周期 ----------

def test_goal_without_arg_shows_hint():
    _, outcome = _run("/goal")
    assert outcome.kind == "block"
    assert "当前没有持久目标" in outcome.text


def test_goal_sets_objective_and_start_task():
    _, outcome = _run("/goal 修复所有失败的测试")
    assert "已设定目标" in outcome.text
    assert goal.is_active()
    assert goal.current().objective == "修复所有失败的测试"
    assert outcome.start_task and "修复所有失败的测试" in outcome.start_task
    assert outcome.refresh_status


def test_goal_status_block():
    goal.set("目标 X", max_turns=5)
    _, outcome = _run("/goal")
    assert outcome.kind == "block"
    assert "目标 X" in outcome.text
    assert "进行中" in outcome.text


def test_goal_pause_resume_clear():
    _run("/goal 目标")
    _, paused = _run("/goal pause")
    assert not goal.is_active() and "已暂停" in paused.text and paused.refresh_status

    _, resumed = _run("/goal resume")
    assert goal.is_active() and resumed.start_task and resumed.refresh_status

    _, cleared = _run("/goal clear")
    assert not goal.is_set() and "已清除" in cleared.text
    assert cleared.refresh_status


def test_goal_resume_without_goal():
    _, outcome = _run("/goal resume")
    assert "没有持久目标" in outcome.text


def test_goal_resume_while_active_kicks_continuation():
    """active 目标（如中断后循环已停）再 resume 也接续一轮，而不是死胡同提示。"""
    _run("/goal 目标")
    _, outcome = _run("/goal resume")
    assert goal.is_active()
    assert outcome.start_task


def test_goal_budget_validation_and_set():
    _run("/goal 目标")
    _, bad = _run("/goal budget abc")
    assert "用法" in bad.text
    _, good = _run("/goal budget 5")
    assert goal.current().max_turns == 5
    assert "5" in good.text


def test_goal_budget_can_be_cancelled():
    """unlimited / off 取消回合上限（默认即为不限）。"""
    _run("/goal 目标")
    _run("/goal budget 5")
    assert not goal.current().unlimited

    _, outcome = _run("/goal budget unlimited")
    assert goal.current().unlimited
    assert "不限" in outcome.text


def test_goal_objective_starting_with_verb_is_not_subcommand():
    """/goal clear the failures 是目标描述而非清除命令（子命令只在单 token 时识别）。"""
    _, outcome = _run("/goal clear the failing tests")
    assert goal.is_active()
    assert goal.current().objective == "clear the failing tests"
    assert outcome.start_task


def test_goal_rejects_overlong_objective():
    _, outcome = _run("/goal " + "长" * (goal.MAX_OBJECTIVE_LEN + 1))
    assert "过长" in outcome.text
    assert not goal.is_set()


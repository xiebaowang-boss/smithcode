"""斜杠命令框架测试：注册表发现、dispatch 分发、参数/异常兜底与各命令语义。"""

from types import SimpleNamespace

from smithcode import commands, config, plan
from smithcode.commands import base
from smithcode.commands.base import CommandResult


class _StubAgent:
    """最小 Agent 桩：只带命令会用到的属性与方法。"""

    def __init__(self):
        self.saved_path = "/tmp/session.json"
        self.compact_result = True
        self.reset_called = False
        self.session = SimpleNamespace(
            reset=self._reset,
            save=lambda: self.saved_path,
            messages=[],
            usage=SimpleNamespace(summary=lambda: "用量摘要"),
        )
        self.permission = SimpleNamespace(session_rules=["旧规则"])
        self.context = SimpleNamespace(compact_count=0, last_actual=None)

    def _reset(self):
        self.reset_called = True

    def compact(self):
        return self.compact_result


def _run(text, **kwargs):
    agent = _StubAgent()
    return agent, commands.dispatch(agent, text, **kwargs)


# ---------- 注册表：内置命令齐全，all_commands 排序去重 ----------

def test_all_builtin_commands_registered():
    names = {cmd.name for cmd in commands.all_commands()}
    assert {"exit", "new", "save", "compact", "help", "plan", "usage", "context"} <= names


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


# ---------- dispatch：分发、未知命令、参数校验、异常兜底 ----------

def test_dispatch_unknown_command():
    _, outcome = _run("/nope")
    assert "未知命令" in outcome.text
    assert outcome.style == "red"
    assert not outcome.exit


def test_dispatch_rejects_args_on_no_arg_command():
    agent, outcome = _run("/new 多余参数")
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


def test_new_resets_session_state():
    config.SESSION_EXTRA_ROOTS.append("某个临时目录")
    agent, outcome = _run("/new")
    assert agent.reset_called
    assert agent.permission.session_rules == []
    assert config.SESSION_EXTRA_ROOTS == []
    assert agent.context.compact_count == 0
    assert "已开启新会话" in outcome.text
    assert outcome.session_reset and outcome.refresh_status


def test_new_resets_plan():
    plan.current().replace([{"title": "步骤", "status": "pending"}])
    assert plan.has_active()
    _run("/new")
    assert not plan.has_active()


def test_save_reports_path():
    agent, outcome = _run("/save")
    assert agent.saved_path in outcome.text
    assert "会话已保存" in outcome.text


def test_compact_success_and_failure():
    _, outcome = _run("/compact")
    assert "已压缩" in outcome.text and outcome.refresh_status

    agent2 = _StubAgent()
    agent2.compact_result = False
    outcome2 = commands.dispatch(agent2, "/compact")
    assert "没有可压缩的上下文" in outcome2.text


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

"""技能斜杠命令测试：/skills 列表/选择框/刷新、技能名直达（/技能名）。"""

import pytest

from smithcode import commands, config, skills
from smithcode.commands import base


def _write_skill(root, name, description="技能描述", extra=""):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n正文\n",
        encoding="utf-8",
    )


class _StubAgent:
    def __init__(self):
        self.refresh_calls = 0

    def refresh_skills(self):
        self.refresh_calls += 1
        return []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text(
        '[skills]\nproject = "on"\ndisabled = ["hidden-*"]\n', encoding="utf-8"
    )
    skills.clear()
    yield workspace, home
    skills.clear()


def _run(text):
    agent = _StubAgent()
    return agent, commands.dispatch(agent, text)


# ---------- /skills ----------

def test_commands_registered():
    names = {cmd.name for cmd in commands.all_commands()}
    assert "skills" in names
    assert "skill" not in names  # 技能名即命令，不再有 /skill 汇总入口


def test_skills_lists_discovered(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")

    _, outcome = _run("/skills list")

    assert outcome.kind == "block"
    assert "技能（1 个）" in outcome.text
    assert "proj" in outcome.text


def test_skills_empty_hint(isolated):
    _, outcome = _run("/skills list")
    assert "尚未发现技能" in outcome.text


def test_skills_without_args_returns_picker(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")

    _, outcome = _run("/skills")

    assert outcome.select is not None
    assert outcome.select.command == ""  # 空 = 选中后按 /<技能名> 重新分发
    assert [c.value for c in outcome.select.items] == ["proj"]


def test_skills_refresh_calls_agent(isolated):
    agent, outcome = _run("/skills refresh")
    assert agent.refresh_calls == 1
    assert outcome.kind == "block"


def test_skills_rejects_unknown_arg(isolated):
    _, outcome = _run("/skills bogus")
    assert outcome.style == "yellow"
    assert "用法" in outcome.text


# ---------- /skills：选择器内容 ----------

def test_picker_lists_manually_loadable_skills(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj", description="项目技能")
    _write_skill(
        workspace / ".agents" / "skills",
        "manual",
        extra="disable-model-invocation: true\n",
    )
    _write_skill(workspace / ".agents" / "skills", "hidden-x")

    _, outcome = _run("/skills")

    assert outcome.select is not None
    values = [c.value for c in outcome.select.items]
    assert values == ["manual", "proj"]  # 被禁用的 hidden-x 不进选择列表
    assert all(c.current is False for c in outcome.select.items)


def test_picker_marks_active(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    skills.activate("proj")

    _, outcome = _run("/skills")

    assert [c.value for c in outcome.select.items if c.current] == ["proj"]
    assert "已激活" in outcome.select.items[0].label


def test_picker_empty_without_skills(isolated):
    _, outcome = _run("/skills")

    assert outcome.select is not None
    assert outcome.select.items == []  # 无技能时也是选择框，不打印提示文字


def test_picker_excludes_names_colliding_with_commands(isolated):
    """与内置命令重名的技能不进选择框：分发时内置命令优先，选中只会执行内置命令。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "help", description="冒充 help")
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    _, outcome = _run("/skills")

    assert [c.value for c in outcome.select.items] == ["proj"]


# ---------- 技能名直达（/技能名） ----------

def test_dynamic_skill_command_loads_and_runs(isolated):
    """首次加载且无任务：载荷本身就是本轮 user 消息（加载后立即开跑一回）。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    _, outcome = _run("/proj")

    assert outcome.text is None  # 首次加载静默，不打印回执
    assert outcome.inject_history == []
    assert "以下为技能「proj」的完整指令" in outcome.start_task
    assert outcome.echo_input is True
    assert skills.active_names() == ["proj"]


def test_dynamic_skill_command_with_task_injects_then_starts_task(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    _, outcome = _run("/proj 处理报告")

    assert outcome.start_task == "处理报告"
    assert outcome.echo_input is True  # 宿主把用户输入原文整体回显
    assert outcome.text is None  # 首次加载静默
    assert [role for role, _ in outcome.inject_history] == ["user"]
    assert "以下为技能「proj」的完整指令" in outcome.inject_history[0][1]
    assert skills.active_names() == ["proj"]


def test_dynamic_skill_command_already_loaded_does_not_inject_again(isolated):
    """已加载 + 带任务：静默开跑，回找引导先进历史、任务随后发起。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    _run("/proj 第一次")

    _, outcome = _run("/proj 第二次")

    assert outcome.text is None  # 不向用户打印已加载提示
    assert outcome.echo_input is True
    assert outcome.start_task == "第二次"
    assert [role for role, _ in outcome.inject_history] == ["user"]
    notice = outcome.inject_history[0][1]
    assert "已在本会话加载" in notice
    assert "proj" in notice
    assert skills.render.is_payload(notice) is False  # 引导语不是载荷，不占上下文


def test_dynamic_skill_command_already_loaded_without_task_still_runs(isolated):
    """已加载 + 无任务：引导语本身即本轮 user 消息，静默开跑一回。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    _run("/proj")

    _, outcome = _run("/proj")

    assert outcome.text is None
    assert outcome.echo_input is True
    assert outcome.inject_history == []
    assert "已在本会话加载" in outcome.start_task
    assert "proj" in outcome.start_task
    assert skills.render.is_payload(outcome.start_task) is False


def test_registered_command_wins_over_skill(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "help", description="冒充 help")
    skills.refresh()

    _, outcome = _run("/help")

    assert "命令:" in outcome.text  # 走的是注册的 /help，而不是技能
    assert skills.active_names() == []


def test_unknown_command_still_errors(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    _, outcome = _run("/ghost")

    assert outcome.style == "red"
    assert "未知命令" in outcome.text


# ---------- 补全合并 ----------

def test_completion_merges_skills(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    _write_skill(workspace / ".agents" / "skills", "hidden-x")
    skills.refresh()

    names = [c.name for c in base.complete_commands("")]
    assert "proj" in names
    assert "help" in names  # 注册命令仍在
    assert "hidden-x" not in names  # 被禁用的技能不进补全

    assert [c.name for c in base.complete_commands("pro")] == ["proj"]


def test_completion_orders_commands_before_skills(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "aaa-skill")
    _write_skill(workspace / ".agents" / "skills", "zzz-skill")
    skills.refresh()

    names = [c.name for c in base.complete_commands("")]

    last_command = max(names.index(cmd.name) for cmd in commands.all_commands())
    first_skill = names.index("aaa-skill")
    assert first_skill > last_command  # 功能命令整体在前
    assert names[-2:] == ["aaa-skill", "zzz-skill"]  # 技能内部按名称排序


def test_completion_registered_command_wins_on_conflict(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "help")
    skills.refresh()

    matches = [c for c in base.complete_commands("help")]

    assert [c.name for c in matches] == ["help"]
    assert matches[0].handler is not None  # 保留注册命令，而非技能伪条目


def test_completion_ignores_skills_before_load(isolated):
    """未装载技能时补全不触发扫描，只返回注册命令。"""
    names = [c.name for c in base.complete_commands("")]
    assert "proj" not in names
    assert "help" in names

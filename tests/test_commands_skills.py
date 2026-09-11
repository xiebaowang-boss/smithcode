"""技能斜杠命令测试：/skills 列表与刷新、/skill 选择器与加载、动态 /技能名 直达。"""

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
    assert {"skills", "skill"} <= names


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
    assert outcome.select.command == "skill"
    assert [c.value for c in outcome.select.items] == ["proj"]


def test_skills_refresh_calls_agent(isolated):
    agent, outcome = _run("/skills refresh")
    assert agent.refresh_calls == 1
    assert outcome.kind == "block"


def test_skills_rejects_unknown_arg(isolated):
    _, outcome = _run("/skills bogus")
    assert outcome.style == "yellow"
    assert "用法" in outcome.text


# ---------- /skill：无参选择器 ----------

def test_skill_without_args_returns_picker(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj", description="项目技能")
    _write_skill(
        workspace / ".agents" / "skills",
        "manual",
        extra="disable-model-invocation: true\n",
    )
    _write_skill(workspace / ".agents" / "skills", "hidden-x")

    _, outcome = _run("/skill")

    assert outcome.select is not None
    assert outcome.select.command == "skill"
    values = [c.value for c in outcome.select.items]
    assert values == ["manual", "proj"]  # 被禁用的 hidden-x 不进选择列表
    assert all(c.current is False for c in outcome.select.items)


def test_skill_picker_marks_active(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    skills.activate("proj")

    _, outcome = _run("/skill")

    assert [c.value for c in outcome.select.items if c.current] == ["proj"]
    assert "已激活" in outcome.select.items[0].label


def test_skill_without_args_and_no_skills_returns_empty_picker(isolated):
    _, outcome = _run("/skill")

    assert outcome.select is not None
    assert outcome.select.items == []  # 无技能时也是选择框，不打印提示文字


# ---------- /skill：带参加载 ----------

def test_skill_unknown_name_is_error(isolated):
    _, outcome = _run("/skill ghost")
    assert outcome.style == "red"
    assert outcome.text.startswith("错误:")


def test_skill_activates(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")

    _, outcome = _run("/skill proj")

    assert outcome.text is None  # 静默激活，不打印"已加载技能"提示
    assert outcome.start_task is None
    assert skills.active_names() == ["proj"]


def test_skill_with_task_starts_task(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")

    _, outcome = _run("/skill proj 帮我处理报告")

    assert outcome.start_task == "帮我处理报告"
    assert outcome.echo_input is True  # 宿主把用户输入原文整体回显
    assert outcome.text is None


# ---------- 动态 /技能名 直达 ----------

def test_dynamic_skill_command_activates(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    _, outcome = _run("/proj")

    assert outcome.text is None  # 静默激活
    assert skills.active_names() == ["proj"]


def test_dynamic_skill_command_with_task_starts_task(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    _, outcome = _run("/proj 处理报告")

    assert outcome.start_task == "处理报告"
    assert outcome.echo_input is True
    assert skills.active_names() == ["proj"]


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

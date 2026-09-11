"""技能发现测试：扫描范围、优先级、宽容跳过与项目级信任门控。"""

import os
from pathlib import Path

import pytest

from smithcode import config
from smithcode.skills import registry


def _write_skill(root, name, description="技能描述", body="技能正文", extra=""):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """隔离工作区与用户配置根；清空技能只读白名单与会话信任。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    registry.reset_session_trust()
    yield workspace, home
    config.set_skill_roots([])
    registry.reset_session_trust()


def test_discovers_project_and_user_skills(isolated):
    workspace, home = isolated
    _write_skill(workspace / ".agents" / "skills", "proj-skill")
    _write_skill(home / "skills", "user-skill")

    result = registry.discover()

    by_name = {s.name: s for s in result.skills}
    assert by_name["proj-skill"].scope == "project"
    assert by_name["proj-skill"].base == workspace / ".agents" / "skills" / "proj-skill"
    assert by_name["proj-skill"].body.strip() == "技能正文"
    assert by_name["user-skill"].scope == "user"
    assert result.diagnostics == []


def test_only_agents_dir_is_scanned_in_project(isolated):
    """P1 有意收窄：项目侧只认 .agents/skills，不兼容 .claude/.smithcode 等目录。"""
    workspace, _ = isolated
    _write_skill(workspace / ".claude" / "skills", "claude-skill")
    _write_skill(workspace / ".smithcode" / "skills", "native-skill")

    names = {s.name for s in registry.discover().skills}

    assert names == set()


def test_priority_config_path_over_project_over_user(isolated, tmp_path):
    workspace, home = isolated
    extra = tmp_path / "extra-skills"
    for root, marker in (
        (extra, "附加"),
        (workspace / ".agents" / "skills", "项目"),
        (home / "skills", "用户"),
    ):
        _write_skill(root, "same", description=marker)

    result = registry.discover(config.SkillsConfig(paths=(str(extra),)))

    skills = [s for s in result.skills if s.name == "same"]
    assert len(skills) == 1
    assert skills[0].scope == "config"
    assert skills[0].description == "附加"
    assert any("被同名技能遮蔽" in d for d in result.diagnostics)


def test_project_overrides_user(isolated):
    workspace, home = isolated
    _write_skill(home / "skills", "same", description="用户")
    _write_skill(workspace / ".agents" / "skills", "same", description="项目")

    skills = registry.discover().skills

    assert skills[0].scope == "project"


def test_skips_skill_without_description(isolated):
    workspace, _ = isolated
    root = workspace / ".agents" / "skills"
    directory = root / "broken"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("---\nname: broken\n---\nbody\n", encoding="utf-8")
    _write_skill(root, "good")

    result = registry.discover()

    assert [s.name for s in result.skills] == ["good"]
    assert any("跳过技能" in d for d in result.diagnostics)


def test_name_falls_back_to_directory_and_warns_on_mismatch(isolated):
    workspace, _ = isolated
    root = workspace / ".agents" / "skills"
    directory = root / "dir-name"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: other-name\ndescription: d\n---\nbody\n", encoding="utf-8"
    )

    result = registry.discover()

    skill = result.skills[0]
    assert skill.name == "other-name"
    assert any("不一致" in w for w in skill.warnings)


def test_disabled_pattern_hides_from_model_but_keeps_record(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "internal-tool")
    _write_skill(workspace / ".agents" / "skills", "public-tool")

    result = registry.discover(config.SkillsConfig(disabled=("internal-*",)))

    by_name = {s.name: s for s in result.skills}
    assert by_name["internal-tool"].disabled is True
    assert by_name["internal-tool"].disabled_by == "internal-*"
    assert by_name["public-tool"].disabled is False


def test_disable_model_invocation_flag(isolated):
    workspace, _ = isolated
    _write_skill(
        workspace / ".agents" / "skills",
        "manual-only",
        extra="disable-model-invocation: true\n",
    )

    skill = registry.discover().skills[0]

    assert skill.model_invocable is False


def test_scan_skips_noise_dirs_and_finds_nested_group(isolated):
    workspace, _ = isolated
    root = workspace / ".agents" / "skills"
    _write_skill(root / "node_modules" / "pkg", "hidden-noise")
    _write_skill(root / "group" / "nested", "nested-skill")

    names = {s.name for s in registry.discover().skills}

    assert names == {"nested-skill"}


def test_scan_does_not_descend_into_found_skill(isolated):
    workspace, _ = isolated
    root = workspace / ".agents" / "skills"
    outer = _write_skill(root, "outer")
    _write_skill(outer, "inner")

    names = {s.name for s in registry.discover().skills}

    assert names == {"outer"}


def test_enabled_false_returns_empty(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "a-skill")

    result = registry.discover(config.SkillsConfig(enabled=False))

    assert result.skills == [] and result.enabled is False


def test_resources_listing_excludes_skill_md(isolated):
    workspace, _ = isolated
    base = _write_skill(workspace / ".agents" / "skills", "with-resources")
    (base / "scripts").mkdir()
    (base / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
    (base / "references").mkdir()
    (base / "references" / "doc.md").write_text("doc", encoding="utf-8")

    resources = registry.list_resources(base)

    assert resources == ["references/doc.md", "scripts/run.py"]
    assert "SKILL.md" not in resources


# ---------- 项目级信任门控 ----------

class _DummyRenderer:
    def __init__(self, answer="n"):
        self.answer = answer
        self.infos = []

    def info(self, text):
        self.infos.append(text)

    def confirm_choice(self, prompt, valid, hint):
        return self.answer


def _project_preview(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    return registry.discover().skills


def test_trust_off_skips_project(isolated):
    preview = _project_preview(isolated)
    diagnostics = []

    assert registry.resolve_project_trust(
        config.SkillsConfig(project="off"), preview, diagnostics
    ) is False
    assert any("project=off" in d for d in diagnostics)


def test_trust_on_loads_without_prompt(isolated):
    preview = _project_preview(isolated)
    assert registry.resolve_project_trust(
        config.SkillsConfig(project="on"), preview, []
    ) is True


def test_trust_ask_non_interactive_fails_closed(isolated, monkeypatch):
    preview = _project_preview(isolated)
    diagnostics = []
    monkeypatch.setattr(
        "smithcode.utils.terminal.confirmations_available", lambda: False
    )

    assert registry.resolve_project_trust(config.SkillsConfig(), preview, diagnostics) is False
    assert any("非交互模式" in d for d in diagnostics)


def test_trust_ask_always_persists(isolated, monkeypatch):
    preview = _project_preview(isolated)
    monkeypatch.setattr(
        "smithcode.utils.terminal.confirmations_available", lambda: True
    )
    monkeypatch.setattr(
        "smithcode.skills.registry.renderer.current", lambda: _DummyRenderer("a")
    )

    assert registry.resolve_project_trust(config.SkillsConfig(), preview, []) is True

    key = registry.project_key(Path(config.WORKSPACE_ROOT))
    assert registry.load_trust().get(key) is True
    # 第二次直接命中信任库，不再弹确认
    assert registry.resolve_project_trust(config.SkillsConfig(), preview, []) is True


def test_trust_ask_once_is_session_only(isolated, monkeypatch):
    preview = _project_preview(isolated)
    monkeypatch.setattr(
        "smithcode.utils.terminal.confirmations_available", lambda: True
    )
    monkeypatch.setattr(
        "smithcode.skills.registry.renderer.current", lambda: _DummyRenderer("y")
    )

    assert registry.resolve_project_trust(config.SkillsConfig(), preview, []) is True
    key = registry.project_key(Path(config.WORKSPACE_ROOT))
    assert registry.load_trust().get(key) is None  # 未落盘
    registry.reset_session_trust()
    monkeypatch.setattr(
        "smithcode.skills.registry.renderer.current", lambda: _DummyRenderer("n")
    )
    assert registry.resolve_project_trust(config.SkillsConfig(), preview, []) is False


def test_trust_denied_diagnostics(isolated, monkeypatch):
    preview = _project_preview(isolated)
    diagnostics = []
    monkeypatch.setattr(
        "smithcode.utils.terminal.confirmations_available", lambda: True
    )
    monkeypatch.setattr(
        "smithcode.skills.registry.renderer.current", lambda: _DummyRenderer("n")
    )

    assert registry.resolve_project_trust(config.SkillsConfig(), preview, diagnostics) is False
    assert any("用户跳过" in d for d in diagnostics)


def test_project_key_uses_git_root(isolated):
    workspace, _ = isolated
    (workspace / ".git").mkdir()
    nested = workspace / "sub" / "deeper"
    nested.mkdir(parents=True)
    assert registry.project_key(nested) == os.path.normcase(str(workspace.resolve()))

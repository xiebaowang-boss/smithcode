"""技能会话状态测试：refresh / reset / activate / 系统提示词渲染。"""

import pytest

from smithcode import config, skills


def _write_skill(root, name, description="技能描述", body="唯一正文标记", extra=""):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    # 项目技能默认 ask 会弹确认；测试里显式信任项目
    (home / "config.toml").write_text(
        '[skills]\nproject = "on"\n', encoding="utf-8"
    )
    skills.clear()
    yield workspace, home
    skills.clear()


def test_refresh_discovers_and_registers_read_roots(isolated):
    workspace, home = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    _write_skill(home / "skills", "user")

    diagnostics = skills.refresh()

    assert diagnostics == []
    assert {s.name for s in skills.model_skills()} == {"proj", "user"}
    roots = {str(r) for r in config.skill_roots()}
    assert str((workspace / ".agents" / "skills").resolve()) in roots
    assert str((home / "skills").resolve()) in roots


def test_render_section_before_load_is_empty(isolated):
    assert skills.render_section() == ""


def test_render_section_contains_catalog_and_active_body(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    catalog = skills.render_section()
    assert "## 可用技能" in catalog
    assert "proj" in catalog
    assert "唯一正文标记" not in catalog  # 第 2 层未激活前不出现

    text = skills.activate("proj")
    assert "已激活" in text
    section = skills.render_section()
    assert "## 已激活技能" in section
    assert "唯一正文标记" in section
    assert "<skill name=" in section


def test_activate_unknown_lists_available(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    text = skills.activate("nope")

    assert text.startswith("错误:")
    assert "proj" in text


def test_activate_is_idempotent(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    skills.activate("proj")
    assert "已激活" in skills.activate("proj")
    assert skills.activate("proj").count("无需重复") == 1
    assert skills.active_names() == ["proj"]


def test_reset_clears_active_but_keeps_discovery(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    skills.activate("proj")

    skills.reset()

    assert skills.active_names() == []
    assert "## 可用技能" in skills.render_section()  # 发现结果保留


def test_manual_only_skill_rejects_model_and_allows_user(isolated):
    workspace, _ = isolated
    _write_skill(
        workspace / ".agents" / "skills",
        "manual",
        extra="disable-model-invocation: true\n",
    )
    skills.refresh()

    assert skills.model_skills() == []
    assert skills.activate("manual").startswith("错误:")
    assert "已加载技能 manual" in skills.activate("manual", by="user")


def test_disabled_skill_cannot_activate(isolated):
    workspace, home = isolated
    (home / "config.toml").write_text(
        '[skills]\nproject = "on"\ndisabled = ["hidden-*"]\n', encoding="utf-8"
    )
    _write_skill(workspace / ".agents" / "skills", "hidden-x")
    _write_skill(workspace / ".agents" / "skills", "visible")
    skills.refresh()

    assert [s.name for s in skills.model_skills()] == ["visible"]
    assert "已被配置禁用" in skills.activate("hidden-x")


def test_refresh_drops_activations_of_removed_skills(isolated):
    workspace, _ = isolated
    base = _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    skills.activate("proj")

    (base / "SKILL.md").unlink()
    skills.refresh()

    assert skills.active_names() == []
    assert skills.get("proj") is None


def test_status_text_lists_sources_and_states(isolated):
    workspace, home = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    _write_skill(home / "skills", "user")
    skills.refresh()
    skills.activate("proj")

    text = skills.status_text()

    assert "技能（2 个）" in text
    assert "项目" in text and "用户" in text
    assert "[已激活]" in text

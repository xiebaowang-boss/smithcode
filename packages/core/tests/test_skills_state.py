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


def test_render_section_is_catalog_only_and_stable_across_loads(isolated):
    """加载技能不改动系统提示词：正文作为载荷返回，提示前缀缓存全程稳定。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    catalog = skills.render_section()
    assert "## 可用技能" in catalog
    assert "proj" in catalog
    assert "唯一正文标记" not in catalog  # 目录段只有第 1 层信息

    payload = skills.activate("proj")
    assert "唯一正文标记" in payload  # 第 2 层载荷由调用方投递进对话
    assert "<skill name=" in payload
    assert skills.render_section() == catalog


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

    first = skills.activate("proj")
    assert "唯一正文标记" in first
    second = skills.activate("proj")
    # 模型通道重复：回已加载提示 + 历史回找指引，不重复注入正文
    assert "唯一正文标记" not in second
    assert "已加载" in second
    assert "对话历史" in second
    assert "read_file" in second
    assert skills.active_names() == ["proj"]


def test_activate_repeat_by_user_returns_sentinel(isolated):
    """用户通道重复：回哨兵供命令层判定，提示语由 render.recall_notice 渲染。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    skills.activate("proj", by="user")

    assert skills.activate("proj", by="user") == "__already_loaded__:proj"
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
    assert "以下为技能「manual」的完整指令" in skills.activate("manual", by="user")


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


def test_prune_active_drops_skills_missing_from_context(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    skills.activate("proj")

    dropped = skills.prune_active([{"role": "user", "content": "无关内容"}])

    assert dropped == ["proj"]
    assert skills.active_names() == []


def test_prune_active_keeps_payload_still_in_context(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    payload = skills.activate("proj")

    dropped = skills.prune_active([{"role": "tool", "content": payload}])

    assert dropped == []
    assert skills.active_names() == ["proj"]


def test_prune_active_treats_truncated_payload_as_dropped(isolated):
    """尾部压缩会把超长 tool 消息头尾截断：截断即视为指令已不完整。"""
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    payload = skills.activate("proj")

    truncated = payload[:200] + "…（已省略中间部分）"
    dropped = skills.prune_active([{"role": "tool", "content": truncated}])

    assert dropped == ["proj"]

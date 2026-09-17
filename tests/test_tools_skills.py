"""use_skill 工具测试：注册元数据、schema 同步、激活与默认权限。"""

import pytest

from smithcode import config, skills
from smithcode.permission import DEFAULT_RULES, evaluate
from smithcode.tools import FUNCTIONS, HIDDEN, PATTERN_ARGS, SERIAL, visible_schemas
from smithcode.tools.skills import sync_schema


def _write_skill(root, name, description="技能描述"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文\n",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    skills.clear()
    HIDDEN.discard("use_skill")
    yield workspace, home
    skills.clear()
    HIDDEN.discard("use_skill")


def test_tool_metadata():
    assert "use_skill" in FUNCTIONS
    assert PATTERN_ARGS["use_skill"] == "name"
    assert SERIAL["use_skill"] is True


def test_default_permission_allows_use_skill():
    assert evaluate("use_skill", "*", DEFAULT_RULES)[2] == "allow"


def test_sync_schema_hides_tool_without_skills():
    skills.refresh()
    sync_schema()

    assert "use_skill" in HIDDEN
    assert "use_skill" not in [s["name"] for s in visible_schemas()]


def test_sync_schema_sets_enum_and_unhides(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    sync_schema()

    schema = next(s for s in visible_schemas() if s["name"] == "use_skill")
    assert schema["parameters"]["properties"]["name"]["enum"] == ["proj"]


def test_use_skill_returns_body_as_result(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()
    sync_schema()

    text = FUNCTIONS["use_skill"](name="proj")

    assert "以下为技能「proj」的完整指令" in text
    assert "正文" in text
    assert 'location="' in text
    assert skills.active_names() == ["proj"]
    assert "## 已激活技能" not in skills.render_section()  # 正文不进系统提示词


def test_use_skill_unknown_name_returns_error(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    text = FUNCTIONS["use_skill"](name="ghost")

    assert text.startswith("错误:")
    assert "proj" in text


def test_use_skill_repeat_call_does_not_duplicate_body(isolated):
    workspace, _ = isolated
    _write_skill(workspace / ".agents" / "skills", "proj")
    skills.refresh()

    first = FUNCTIONS["use_skill"](name="proj")
    second = FUNCTIONS["use_skill"](name="proj")

    assert "正文" in first
    assert "正文" not in second
    assert "无需重复" in second


def test_use_skill_truncates_oversized_payload(isolated, monkeypatch):
    """载荷超上限时截断并给出读取指引（技能文件仍可用 read_file 取回）。"""
    workspace, _ = isolated
    directory = workspace / ".agents" / "skills" / "big"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: big\ndescription: d\n---\n" + "长" * 3000, encoding="utf-8"
    )
    monkeypatch.setattr(config, "MAX_TOOL_OUTPUT", 500)
    skills.refresh()

    text = FUNCTIONS["use_skill"](name="big")

    assert len(text) <= 500
    assert "已截断" in text
    assert "read_file" in text

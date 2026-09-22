"""Session 测试：会话 id 轮换与系统提示词同步（含持久目标动态段）。"""

import os
import uuid

import pytest

from smithcode import config, goal
from smithcode.session import Session


@pytest.fixture(autouse=True)
def _fresh_goal():
    goal.reset()
    yield
    goal.reset()


def test_construction_rotates_session_id():
    first = config.SESSION_ID
    Session()
    assert config.SESSION_ID != first
    # uuid4().hex：32 位十六进制
    assert len(config.SESSION_ID) == 32
    uuid.UUID(config.SESSION_ID)


def test_reset_rotates_session_id():
    Session()
    before = config.SESSION_ID
    Session().reset()
    assert config.SESSION_ID != before


def test_sync_system_inserts_once_and_keeps_content_stable():
    session = Session()
    session.sync_system()
    assert session.messages[0]["role"] == "system"
    assert "Smith Code" in session.messages[0]["content"]
    first = session.messages[0]["content"]
    session.sync_system()  # 内容未变不重建（保护提示缓存）
    assert len(session.messages) == 1
    assert session.messages[0]["content"] == first


def test_sync_system_refreshes_goal_section():
    session = Session()
    session.sync_system()
    goal.set("迁移模块到新 API")
    session.sync_system()
    assert "## 当前持久目标" in session.messages[0]["content"]
    assert "迁移模块到新 API" in session.messages[0]["content"]
    goal.clear()
    session.sync_system()
    assert "## 当前持久目标" not in session.messages[0]["content"]


def test_sync_system_includes_skills_catalog_only(tmp_path, monkeypatch):
    """技能装载后只有目录进系统提示词；加载正文不改动 messages[0]（前缀缓存稳定）。"""
    from smithcode import skills

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (home / "config.toml").write_text('[skills]\nproject = "on"\n', encoding="utf-8")
    skill_dir = workspace / ".agents" / "skills" / "proj"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: proj\ndescription: 描述\n---\n正文标记\n", encoding="utf-8"
    )

    skills.clear()
    try:
        skills.refresh()
        session = Session()
        session.sync_system()
        catalog = session.messages[0]["content"]
        assert "## 可用技能" in catalog
        assert "正文标记" not in catalog

        payload = skills.activate("proj")
        assert "正文标记" in payload  # 正文作为载荷返回，由调用方投递进对话
        session.sync_system()
        assert session.messages[0]["content"] == catalog

        skills.reset()
        session.sync_system()
        assert session.messages[0]["content"] == catalog
    finally:
        skills.clear()


def test_sync_system_includes_instructions_section(tmp_path, monkeypatch):
    """AGENTS.md 装载后进系统提示词；会话中途修改不重载，边界 refresh 后才生效。"""
    from smithcode import instructions

    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    (workspace / "AGENTS.md").write_text("初始约定标记", encoding="utf-8")

    instructions.reset()
    try:
        session = Session()
        session.sync_system()
        assert "## 项目约定" in session.messages[0]["content"]
        assert "初始约定标记" in session.messages[0]["content"]

        # 会话中途修改：不重载（保护提示前缀缓存）
        (workspace / "AGENTS.md").write_text("改后的约定标记", encoding="utf-8")
        stat = (workspace / "AGENTS.md").stat()
        os.utime(workspace / "AGENTS.md", (stat.st_atime + 5, stat.st_mtime + 5))
        session.sync_system()
        assert "初始约定标记" in session.messages[0]["content"]
        assert "改后的约定标记" not in session.messages[0]["content"]

        # 会话边界（模拟 Agent.start / new_session / resume 的 refresh）重新装载
        assert instructions.refresh() is True
        session.sync_system()
        assert "改后的约定标记" in session.messages[0]["content"]
        assert "初始约定标记" not in session.messages[0]["content"]

        (workspace / "AGENTS.md").unlink()
        instructions.refresh()
        session.sync_system()
        assert "## 项目约定" not in session.messages[0]["content"]
    finally:
        instructions.reset()


# ---------- 持久化绑定与原地恢复 ----------

@pytest.fixture
def _isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    return home


def test_message_log_persists_appends(_isolated_home):
    from smithcode.sessions import SessionStore, load, summary_from_path

    store = SessionStore.create()
    session = Session(store=store)
    assert config.SESSION_ID == store.id  # 绑定时采用转录 id
    session.add("user", "你好")
    session.sync_system()  # system 不入日志（恢复时按最新提示词重建）
    session.add("assistant", "在")  # 消息的写入路径只有 add（发事件）
    store.close()

    loaded = load(summary_from_path(store.path))
    assert [m["role"] for m in loaded.messages] == ["user", "assistant"]


def test_set_compacted_writes_checkpoint(_isolated_home):
    from smithcode.sessions import SessionStore, load, summary_from_path

    store = SessionStore.create()
    session = Session(store=store)
    session.add("user", "旧")
    summary = {"role": "user", "content": "<context-summary>摘要</context-summary>"}
    tail = [{"role": "user", "content": "新"}]
    session.set_compacted(summary, tail, before=10, after=5)
    assert session.messages == [summary, *tail]

    loaded = load(summary_from_path(store.path))
    assert loaded.compact_count == 1
    assert loaded.messages[1]["content"] == "新"


def test_restore_state_is_in_place(_isolated_home):
    session = Session()
    identity = session
    session.restore_state(
        [{"role": "user", "content": "历史"}],
        {"created": 123.0},
        title="标题",
        title_source="user",
    )
    assert session is identity
    assert session.created_at == 123.0
    assert session.title == "标题"


def test_set_title_auto_does_not_override_user(_isolated_home):
    from smithcode.sessions import SessionStore

    session = Session(store=SessionStore.create())
    session.set_title("用户命名", source="user")
    session.set_title("自动命名", source="auto")
    assert session.title == "用户命名"

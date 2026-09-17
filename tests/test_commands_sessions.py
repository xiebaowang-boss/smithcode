"""会话命令：/sessions（列表/切换/删除）/rename /new [名称] 与选择器降级。"""

import os
import re
from types import SimpleNamespace

import pytest

from smithcode import commands, config, goal, plan
from smithcode.sessions import SessionStore


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    goal.reset()
    plan.reset()
    yield
    goal.reset()
    plan.reset()


def _store_with(prompt="测试会话", title="", session_id=None):
    store = SessionStore(session_id) if session_id else SessionStore.create()
    store.append_message({"role": "user", "content": prompt})
    if title:
        store.append_title(title)
    store.close()
    return store


class _StubAgent:
    def __init__(self):
        self.resumed = []
        self.renamed = None
        self.reset_called = False
        self.session = SimpleNamespace(save=lambda: "会话转录路径")

    def resume(self, summary):
        self.resumed.append(summary.id)
        return SimpleNamespace(
            session_id=summary.id,
            message_count=2,
            title=summary.title,
            repair="none",
            bad_lines=0,
            model="",
        )

    def rename_session(self, title):
        self.renamed = title
        return True

    def new_session(self):
        self.reset_called = True


def _run(text, agent=None):
    agent = agent or _StubAgent()
    return agent, commands.dispatch(agent, text)


def test_sessions_lists_current_project():
    store = _store_with(title="会话甲")
    _, outcome = _run("/sessions list")
    assert "会话甲" in outcome.text
    assert store.id[:8] in outcome.text
    assert outcome.kind == "block"


def test_sessions_delete():
    store = _store_with()
    _, outcome = _run(f"/sessions delete {store.id[:8]}")
    assert "已删除" in outcome.text
    assert not store.path.exists()


def test_sessions_without_args_returns_select():
    store = _store_with()
    _, outcome = _run("/sessions")
    assert outcome.select is not None
    assert outcome.select.command == "sessions"
    assert len(outcome.select.items) == 1
    assert outcome.select.size == "large"  # 会话行较长，指定大档宽度
    item = outcome.select.items[0]
    assert item.description == store.id[:8]  # 短 id 紧跟标题（不再含模型）
    assert re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}", item.trailing)  # 时间右对齐列


def test_sessions_sorted_by_updated_desc():
    """默认按更新时间倒序：后写入的排在最前。"""
    older = _store_with(prompt="旧会话")
    newer = _store_with(prompt="新会话")
    os.utime(older.path, (1_600_000_000, 1_600_000_000))  # 显式定住 mtime，避免同秒并列
    os.utime(newer.path, (1_700_000_000, 1_700_000_000))
    _, outcome = _run("/sessions")
    assert [item.value for item in outcome.select.items] == [newer.id[:8], older.id[:8]]


def test_sessions_without_history_notice():
    _, outcome = _run("/sessions")
    assert outcome.select is None
    assert "还没有历史会话" in outcome.text


def test_sessions_by_index_switches():
    _store_with("第一个")
    agent, outcome = _run("/sessions 1")
    assert agent.resumed
    assert outcome.session_resume is True
    assert "已切换到会话" in outcome.text


def test_sessions_by_prefix_switches():
    store = _store_with()
    agent, outcome = _run(f"/sessions {store.id[:8]}")
    assert agent.resumed == [store.id]
    assert outcome.refresh_status is True


def test_sessions_switch_reports_recorded_model(monkeypatch):
    """转录里的模型与当前不同：切换提示点出，且不悄悄改全局模型。"""
    store = _store_with()
    monkeypatch.setattr(config, "MODEL", "current-model")

    class _ModelStub(_StubAgent):
        def resume(self, summary):
            report = super().resume(summary)
            report.model = "other-model"
            return report

    _, outcome = _run(f"/sessions {store.id[:8]}", agent=_ModelStub())
    assert "other-model" in outcome.text
    assert "current-model" in outcome.text
    assert config.MODEL == "current-model"


def test_sessions_ambiguous_prefix_reports():
    _store_with(session_id="a" * 32)
    _store_with(session_id="ab" + "0" * 30)
    agent, outcome = _run("/sessions a")
    assert "不唯一" in outcome.text
    assert not agent.resumed


def test_rename_sets_title():
    agent, outcome = _run("/rename 新名字")
    assert agent.renamed == "新名字"
    assert "新名字" in outcome.text


def test_new_with_name_renames():
    agent, outcome = _run("/new 迁移项目")
    assert agent.reset_called is True
    assert agent.renamed == "迁移项目"
    assert outcome.session_reset is True

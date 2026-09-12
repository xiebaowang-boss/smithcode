"""Agent 会话恢复：持久化往返、崩溃修复、投影恢复与安全例外。"""

import json

import pytest

from smithcode import config, goal, plan, skills
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.sessions import SessionStore, list_sessions, load, summary_from_path
from smithcode.tools import files as files_mod


class FakeLLM:
    """一步返回最终回复；接受 model 关键字（标题线程会用）。"""

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None, model=None):
        self.calls += 1
        yield ("message", {"role": "assistant", "content": "最终回复"})


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    # 测试默认关闭自动标题（后台线程会引入不确定性），标题单测直接调纯逻辑
    (home / "config.toml").write_text(
        "[sessions]\nauto_title = false\ncleanup_days = 0\n", encoding="utf-8"
    )
    goal.reset()
    plan.reset()
    skills.clear()
    config.SESSION_EXTRA_ROOTS.clear()
    files_mod.READ_FILES.clear()
    yield
    goal.reset()
    plan.reset()
    skills.clear()
    config.SESSION_EXTRA_ROOTS.clear()
    files_mod.READ_FILES.clear()


def _make_agent(monkeypatch, **kwargs) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    return Agent(session=Session(), persist=True, **kwargs)


def test_run_persists_and_resume_roundtrip(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent.run("你好")
    store = agent.session.store
    assert store.path.is_file()
    first_line = store.path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(first_line)["t"] == "meta"

    # 新 Agent（同一工作区）按 id 恢复：历史一致、会话 id 沿用
    resumed = _make_agent(monkeypatch)
    report = resumed.resume(store.id)
    assert config.SESSION_ID == store.id
    assert report.message_count >= 2
    assistant_texts = [
        m.get("content") for m in resumed.session.messages if m.get("role") == "assistant"
    ]
    assert assistant_texts[-1] == "最终回复"

    # 恢复后继续对话：新消息追加到同一转录
    resumed.run("继续")
    reloaded = load(summary_from_path(store.path))
    assert reloaded.messages[-1]["content"] == "最终回复"
    assert any(m.get("content") == "继续" for m in reloaded.messages)


def test_resume_repairs_dangling_tool_calls_and_persists(monkeypatch):
    store = SessionStore.create()
    store.append_message({"role": "user", "content": "做任务"})
    store.append_message({
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call_x",
            "type": "function",
            "function": {"name": "list_dir", "arguments": "{}"},
        }],
    })
    store.close()

    agent = _make_agent(monkeypatch)
    report = agent.resume(store.path.stem)
    assert report.repair == "appended"
    tool_msgs = [m for m in agent.session.messages if m.get("role") == "tool"]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "call_x"
    # 修复结果已落盘：重新加载不再需要修复
    reloaded = load(summary_from_path(store.path))
    assert reloaded.repair == "none"


def test_resume_restores_state_but_resets_authorizations(monkeypatch, tmp_path):
    agent = _make_agent(monkeypatch)
    goal.set("把模块迁移到新 API")
    plan.current().replace([{"title": "第一步", "status": "in_progress"}])
    agent._persist_turn()  # 写入 state 投影缓存
    session_id = agent.session.store.id

    # 模拟上一进程的会话级授权：恢复时必须全部丢弃
    resumed = _make_agent(monkeypatch)
    resumed.permission.session_rules.append(("write_file", "*", "allow"))
    config.SESSION_EXTRA_ROOTS.append(str(tmp_path))
    files_mod.READ_FILES.add(str(tmp_path / "b.py"))
    report = resumed.resume(session_id)

    assert report.title == ""
    assert goal.is_active()
    assert goal.current().objective == "把模块迁移到新 API"
    assert goal.current().turns == 0  # 回合计数有意重置
    assert [item["title"] for item in plan.current().items] == ["第一步"]
    assert resumed.permission.session_rules == []
    assert config.SESSION_EXTRA_ROOTS == []
    assert not files_mod.READ_FILES


def test_new_session_keeps_old_transcript(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent.run("第一轮")
    old_id = agent.session.store.id
    old_path = agent.session.store.path

    agent.new_session()
    assert agent.session.store.id != old_id
    assert old_path.is_file()  # 旧会话保留在磁盘，仍可恢复

    report = agent.resume(old_id)
    assert report.message_count >= 2


def test_rename_sets_user_title_and_auto_cannot_override(monkeypatch):
    agent = _make_agent(monkeypatch)
    agent.run("你好")
    assert agent.rename_session("重构会话管理") is True
    agent.session.set_title("自动标题", source="auto")  # 用户标题优先
    assert agent.session.title == "重构会话管理"

    summaries = {item.id: item for item in list_sessions(limit=0)}
    entry = summaries[agent.session.store.id]
    assert entry.title == "重构会话管理"
    assert entry.display_name == "重构会话管理"


def test_no_persistence_by_default(monkeypatch, tmp_path):
    """persist=False（测试与 --no-session-persistence）不产生任何文件。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    agent = Agent(session=Session())
    agent.run("你好")
    assert agent.session.store is None
    projects = tmp_path / "home" / "projects"
    assert not projects.exists() or not any(projects.rglob("*.jsonl"))

"""Agent 会话恢复：持久化往返、崩溃修复、投影恢复与安全例外。"""

import asyncio
import json
import os
from pathlib import Path

import pytest

from smithcode import config, goal, instructions, plan, skills
from smithcode.agent import Agent
from smithcode.session import Session
from smithcode.sessions import SessionStore, list_sessions, load, summary_from_path
from smithcode.tools import FUNCTIONS
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
    instructions.reset()
    config.SESSION_EXTRA_ROOTS.clear()
    files_mod.READ_FILES.clear()
    yield
    goal.reset()
    plan.reset()
    skills.clear()
    instructions.reset()
    config.SESSION_EXTRA_ROOTS.clear()
    files_mod.READ_FILES.clear()


def _make_agent(monkeypatch, **kwargs) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    return Agent(session=Session(), persist=True, **kwargs)


def test_run_persists_and_resume_roundtrip(monkeypatch):
    agent = _make_agent(monkeypatch)
    asyncio.run(agent.run("你好"))
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
    asyncio.run(resumed.run("继续"))
    reloaded = load(summary_from_path(store.path))
    assert reloaded.messages[-1]["content"] == "最终回复"
    assert any(m.get("content") == "继续" for m in reloaded.messages)


class _ToolThenAnswerLLM:
    """第一轮请求一个工具调用，第二轮给最终回复。"""

    def __init__(self):
        self.calls = 0

    def chat_stream(self, messages, tools=None, model=None):
        self.calls += 1
        if self.calls == 1:
            yield ("message", {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_probe",
                    "type": "function",
                    "function": {"name": "checkpoint_probe", "arguments": "{}"},
                }],
            })
        else:
            yield ("message", {"role": "assistant", "content": "完成"})


def test_fsync_checkpoints_bracket_tool_execution(monkeypatch):
    """fsync 只在两个语义点发生：工具执行前（副作用屏障）与每轮结束。

    普通的逐条追加只 flush：fsync 的成本留给「错了就没法挽回」的时刻。
    """
    events = []

    def probe():
        events.append("tool")
        return "ok"

    monkeypatch.setitem(FUNCTIONS, "checkpoint_probe", probe)
    monkeypatch.setattr(os, "fsync", lambda fd: events.append("fsync"))
    monkeypatch.setattr("smithcode.agent.LLMClient", _ToolThenAnswerLLM)
    agent = Agent(session=Session(), persist=True)
    # 假工具不在权限规则表内，默认 ask 会弹确认；测试统一放行
    monkeypatch.setattr(agent.permission, "check", lambda name, args, content=None: True)

    asyncio.run(agent.run("跑个工具"))

    assert "tool" in events, "工具应当被执行"
    assert events[: events.index("tool")] == ["fsync"], "工具执行前必须先 fsync"
    assert events[-1] == "fsync", "每轮结束还要再 fsync 一次"
    assert events.count("fsync") >= 2


def test_run_records_model_and_resume_reports_it(monkeypatch):
    """每轮把实际模型写进 t=model；恢复把「上次使用模型」交回宿主且不改全局模型。"""
    monkeypatch.setattr(config, "MODEL", "model-a")
    agent = _make_agent(monkeypatch)
    asyncio.run(agent.run("你好"))
    monkeypatch.setattr(config, "MODEL", "model-b")  # 中途 /model 切换
    asyncio.run(agent.run("再问"))
    store = agent.session.store
    store.close()

    records = [
        json.loads(line) for line in store.path.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["model"] for r in records if r["t"] == "model"] == ["model-a", "model-b"]

    resumed = _make_agent(monkeypatch)
    report = resumed.resume(store.id)
    assert report.model == "model-b"  # 上次使用的模型交回宿主
    assert config.MODEL == "model-b"  # 恢复不悄悄改全局模型


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
    asyncio.run(agent.run("第一轮"))
    old_id = agent.session.store.id
    old_path = agent.session.store.path

    agent.new_session()
    assert agent.session.store.id != old_id
    assert old_path.is_file()  # 旧会话保留在磁盘，仍可恢复

    report = agent.resume(old_id)
    assert report.message_count >= 2


def test_rename_sets_user_title_and_auto_cannot_override(monkeypatch):
    agent = _make_agent(monkeypatch)
    asyncio.run(agent.run("你好"))
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
    asyncio.run(agent.run("你好"))
    assert agent.session.store is None
    projects = tmp_path / "home" / "projects"
    assert not projects.exists() or not any(projects.rglob("*.jsonl"))


# ---------- 项目指令：会话边界装载 ----------


def _rewrite(path, text):
    """重写指令文件并前推 mtime，规避文件系统时间戳精度（等长内容也能触发重载）。"""
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime + 5, st.st_mtime + 5))


def test_instructions_load_once_per_session_and_reload_on_new(monkeypatch):
    """会话中途修改 AGENTS.md 不重载（保护提示前缀缓存）；/new 边界重新装载。"""
    workspace = Path(config.WORKSPACE_ROOT)
    (workspace / "AGENTS.md").write_text("约定 v1", encoding="utf-8")

    agent = _make_agent(monkeypatch)
    asyncio.run(agent.run("你好"))
    assert "约定 v1" in agent.session.messages[0]["content"]

    _rewrite(workspace / "AGENTS.md", "约定 v2")
    asyncio.run(agent.run("继续"))
    assert "约定 v2" not in agent.session.messages[0]["content"]

    agent.new_session()
    agent.session.sync_system()
    assert "约定 v2" in agent.session.messages[0]["content"]


def test_resume_reloads_instructions(monkeypatch):
    """恢复即会话边界：system 段按磁盘最新内容重建项目约定。"""
    workspace = Path(config.WORKSPACE_ROOT)
    (workspace / "AGENTS.md").write_text("恢复前约定", encoding="utf-8")

    agent = _make_agent(monkeypatch)
    asyncio.run(agent.run("你好"))
    store_id = agent.session.store.id
    assert "恢复前约定" in agent.session.messages[0]["content"]

    _rewrite(workspace / "AGENTS.md", "恢复后约定")
    resumed = _make_agent(monkeypatch)
    resumed.resume(store_id)
    assert "恢复后约定" in resumed.session.messages[0]["content"]
    assert "恢复前约定" not in resumed.session.messages[0]["content"]

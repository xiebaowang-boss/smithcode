"""事件溯源的验收断言：**重放 == 在线**，且持久事件一条不漏。

这两条是"日志是唯一真相源"的可证伪形式：

1. **等价性**：同一会话跑完之后，把日志重放一遍折叠出的视图，必须与在线跑出来的
   视图**逐字段相等**——否则"真相源"是假的（重放出来的会话跟用户当时看到的不一样）。
   等价靠的是两边走同一个折叠函数（`sessions/project.py`），换句话说：折叠规则里
   出现"只有在某个路径才生效"的分支，这条测试就会红。
2. **完备性**：跑一轮富流程（工具调用 + 会话状态 + 中断），日志里必须出现**所有**
   durable 事件——判据不用手写清单，而是拿总线上真的发过的 durable 事件去比对：
   声明为持久却没落盘，就是漏记（新增事件时最容易发生，且不会让功能测试变红）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode import config, goal, instructions, plan, sandbox, skills
from smithcode.agent import Agent
from smithcode.event.catalog import MessageEnd
from smithcode.event.envelope import wrap
from smithcode.session import Session
from smithcode.sessions import SessionStore, load, summary_from_path
from smithcode.tools import FUNCTIONS
from smithcode.tools import files as files_mod


class ScriptedLLM:
    """脚本化模型：按顺序回放（工具调用 → 最终回复）。"""

    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None, model=None):
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "结束"}
        yield ("message", msg)


def _text(content):
    return {"role": "assistant", "content": content}


def _tool(name, args, call_id="1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """独立家目录 + 工作区 + 关闭自动标题（后台线程会引入不确定性）。"""
    home = tmp_path / "home"
    workspace = tmp_path / "ws"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    (home / "config.toml").write_text(
        "[sessions]\nauto_title = false\ncleanup_days = 0\n", encoding="utf-8"
    )
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)
    goal.reset()
    plan.reset()
    skills.clear()
    instructions.reset()
    sandbox.current().session_extra.clear()
    files_mod.READ_FILES.clear()
    yield
    goal.reset()
    plan.reset()
    skills.clear()
    instructions.reset()
    sandbox.current().session_extra.clear()
    files_mod.READ_FILES.clear()


def _agent(monkeypatch, script, **kwargs) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(script))
    return Agent(session=Session(), persist=True, **kwargs)


def _log_types(path) -> list[str]:
    """日志里的类型名（去掉版本号）。"""
    types = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        name = str(record["type"])
        base, _, version = name.rpartition(".")
        types.append(base if base and version.isdigit() else name)
    return types


# ---------- 1. 重放等价性 ----------


def test_replay_equals_online_view(monkeypatch, tmp_path):
    """跑两轮（含工具调用）后重放日志：消息/标题/模型与在线视图逐字段相等。"""
    tool_file = tmp_path / "ws" / "a.txt"
    tool_file.write_text("内容", encoding="utf-8")
    script = [
        _tool("read_file", {"path": "a.txt"}, "t1"),
        _text("读完了"),
        _text("第二轮的回复"),
    ]
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda **kwargs: "ok")
    agent = _agent(monkeypatch, script)

    seen = []
    agent.events.subscribe(seen.append)

    asyncio.run(agent.run("第一句"))
    agent.rename_session("测试标题")  # 标题事件也进日志
    asyncio.run(agent.run("第二句"))

    online = [dict(m) for m in agent.session.messages if m.get("role") != "system"]
    path = agent.session.store.path
    agent.session.store.close()

    loaded = load(summary_from_path(path))

    assert loaded.messages == online  # 消息逐字段相等（含 tool_calls / tool_call_id）
    assert (loaded.title, loaded.title_source) == (
        agent.session.title, agent.session.title_source,
    )
    assert loaded.model == agent.last_turn.model
    assert loaded.id == agent.session.id
    # 折叠进度：日志里的事件全被折叠过（没有"看不懂就跳过"的类型）
    assert loaded.bad_lines == 0


def test_replay_equals_online_view_after_compaction(monkeypatch, tmp_path):
    """压缩过的会话同样等价：重放折叠到 `HistoryCompacted` 得到同一份历史。"""
    agent = _agent(monkeypatch, [_text("回复一"), _text("回复二")])
    monkeypatch.setattr(config, "CONTEXT_TOKEN_BUDGET", 500)
    monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 1)
    asyncio.run(agent.run("第一句"))
    summary = {"role": "user", "content": "<context-summary>摘要</context-summary>"}
    agent.session.set_compacted(summary, [{"role": "user", "content": "近期"}],
                                before=100, after=10)

    online = [dict(m) for m in agent.session.messages if m.get("role") != "system"]
    path = agent.session.store.path
    agent.session.store.close()

    loaded = load(summary_from_path(path))

    assert loaded.messages == online
    assert loaded.compact_count == 1


# ---------- 2. 持久事件完备性 ----------


def test_every_durable_event_reaches_the_log(monkeypatch, tmp_path):
    """跑一轮富流程：总线上发过的 durable 事件必须**一条不漏**地出现在日志里。

    判据不用手写清单：拿总线记录去比对——所以新增 durable 事件却忘了落盘时，
    这条会红（那类漏记不会让任何功能测试变红）。
    """
    monkeypatch.setitem(FUNCTIONS, "fake_tool", lambda **kwargs: "ok")
    agent = _agent(monkeypatch, [_tool("fake_tool", {}, "t1"), _text("完成")])
    seen = []
    agent.events.subscribe(seen.append)

    asyncio.run(agent.run("做点事"))
    agent.session.record(MessageEnd(message={"role": "tool", "content": "手记一条"}))
    path = agent.session.store.path
    agent.session.store.close()

    emitted = {env.type for env in seen if env.durable}
    written = set(_log_types(path))

    assert emitted, "这一轮应当产生持久事件"
    missing = emitted - written
    assert not missing, f"声明为持久却没落盘的 event: {sorted(missing)}"
    # 易失事件不该进日志（日志只留骨架）
    volatile = {env.type for env in seen if not env.durable}
    assert not (volatile & written), f"易失事件混进了日志: {sorted(volatile & written)}"


def test_volatile_events_never_hit_the_log(monkeypatch, tmp_path):
    """流式增量、通知、忙碌态是易失的：重连时丢掉即可（重放骨架就能重建视图）。"""
    agent = _agent(monkeypatch, [_text("回复")])
    asyncio.run(agent.run("你好"))
    path = agent.session.store.path
    agent.session.store.close()

    types = _log_types(path)
    assert "session.message.ended" in types  # 骨架在
    assert "session.message.delta" not in types
    assert "session.notice" not in types


# ---------- 3. 崩溃收尾也是事件 ----------


def test_crash_repair_is_recorded_as_an_event(monkeypatch, tmp_path):
    """崩溃收尾（悬空工具调用补占位）**写回日志**：下一次恢复就是合法历史。"""
    store = SessionStore.create()
    from smithcode.event.catalog import SessionCreated

    store.append_event(wrap(SessionCreated(cwd=store.cwd, model=store.model),
                            session_id=store.id))
    store.append_event(wrap(MessageEnd(
        message={"role": "assistant", "content": "", "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "fake", "arguments": "{}"}},
        ]},
    )))
    store.close()

    first = load(summary_from_path(store.path))
    assert first.repair == "appended"
    # 占位结果作为事件写进了日志
    assert "session.message.ended" in _log_types(store.path)
    reloaded = load(summary_from_path(store.path))
    assert reloaded.repair == "none"  # 第二次恢复：历史已合法
    assert reloaded.messages[-1]["role"] == "tool"

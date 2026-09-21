"""状态事件：retry 走事件通道（无通道退回渲染器）+ compaction 成对收发。

`StatusChanged` / `StatusCleared` 的两条使用场景各有一个「消费者看得见」的路径：
- retry 由 `llm/client.py` 在退避前发（那里拿不到 Agent，走 `agent/emitter.py` 的
  ContextVar 通道；没有通道就退回渲染器直调，两条路径可观测结果一致）；
- compaction 由 `Agent.compact()` 在摘要请求前后发（有通道就是事件）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx2
import pytest

from smithcode import config
from smithcode.agent import Agent, emitter
from smithcode.event.catalog import StatusChanged, StatusCleared
from smithcode.llm import client as client_mod
from smithcode.llm.client import LLMClient
from smithcode.session import Session

DROP = "对端断开"


class _FakeView:
    """只记录重试相关调用的渲染后端。"""

    def __init__(self):
        self.retries = []
        self.finished = 0

    def retry_started(self, state, owner=None):
        self.retries.append(state)

    def retry_finished(self, owner=None):
        self.finished += 1


def _patch_retry(monkeypatch, view=None) -> None:
    monkeypatch.setattr(client_mod.config, "MAX_RETRIES", 2)
    monkeypatch.setattr(client_mod.retry_mod, "wait", lambda state: None)  # 不真的退避


def _failing_client() -> LLMClient:
    """第一次流被掐断、第二次成功：正好走一遍重试钩子。"""
    llm = LLMClient(
        api_key="test", base_url=None, timeout=1.0, default_model="m",
        client_factory=lambda **kwargs: SimpleNamespace(),
    )

    def stream_once(kwargs):
        yield ("message", {"role": "assistant", "content": "答案"})

    llm._stream_once = stream_once
    return llm


def _one_retry_client() -> LLMClient:
    llm = _failing_client()
    calls = []

    def stream_once(kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx2.RemoteProtocolError(DROP)
        yield ("message", {"role": "assistant", "content": "答案"})

    llm._stream_once = stream_once
    return llm


def test_emitter_channel_round_trip():
    """通道语义：没挂载时 emit 返回 False（调用方据此退回旧路径）。"""
    assert emitter.emit(StatusCleared(kind="retry")) is False

    seen: list = []
    token = emitter.activate(seen.append)
    try:
        assert emitter.emit(StatusChanged(kind="retry", text="重试 1/2")) is True
        assert emitter.current() is not None  # 通道已挂载（绑定方法每次访问都是新对象，不比 is）
    finally:
        emitter.reset(token)

    assert [type(event).__name__ for event in seen] == ["StatusChanged"]
    assert emitter.current() is None
    assert emitter.emit(StatusCleared(kind="retry")) is False


def test_retry_emits_status_events_when_channel_exists(monkeypatch):
    """有事件通道：重试进度走 StatusChanged/Cleared，不再直调渲染器。"""
    view = _FakeView()
    _patch_retry(monkeypatch, view)
    seen: list = []
    token = emitter.activate(seen.append)
    try:
        list(_one_retry_client().chat_stream([{"role": "user", "content": "问题"}]))
    finally:
        emitter.reset(token)

    assert [type(event).__name__ for event in seen] == ["StatusChanged", "StatusCleared"]
    started, cleared = seen
    # 这条走 emitter 通道：记录器直接拿到**载荷**（不经总线信封）
    assert started.kind == cleared.kind == "retry"
    assert started.owner is not None  # owner 带上：前台与后台标题各自的态互不误清
    assert started.payload is not None  # RetryState 原样透传（TUI 靠它渲染序号与倒计时）
    assert view.retries == [] and view.finished == 0  # 不得重复上报


# ---------- compaction ----------


class _SummaryLLM:
    """压缩用的假客户端：`_complete` 走它，返回合格的摘要模板。"""

    def __init__(self, text):
        self.text = text

    def chat_stream(self, messages, tools=None, **kwargs):
        yield ("message", {"role": "assistant", "content": self.text})


SUMMARY = "## 目标\n重构 agent 循环\n## 下一步\n\"跑测试确认\""


def _over_threshold_agent(monkeypatch, summary: str) -> Agent:
    """历史里塞够中段（能被压缩）+ 近期尾部；摘要返回指定文本。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: _SummaryLLM(summary))
    agent = Agent(session=Session())
    filler = [{"role": "user", "content": "旧内容 " * 200},
              {"role": "assistant", "content": "旧回复 " * 200}]
    agent.session.messages = agent.session.messages + filler * 4
    monkeypatch.setattr(config, "COMPACT_KEEP_TOKENS", 1)  # 尾部只留极少 → 中段非空
    return agent


def test_compact_emits_status_pair(monkeypatch):
    agent = _over_threshold_agent(monkeypatch, SUMMARY)
    seen: list = []
    agent.events.subscribe(seen.append)

    assert asyncio.run(agent.compact()) is True

    kinds = [type(env.data).__name__ for env in seen]
    # 压缩期间的三种事实：进度（`Compaction*`）、**历史被替换**（`HistoryCompacted`，
    # 可回放的骨架）、结果文案（Notice）。三者分开，前端各自消费。
    assert kinds == ["CompactionStarted", "HistoryCompacted", "CompactionEnded", "Notice"]
    assert seen[1].data.before_tokens > seen[1].data.after_tokens  # 确实压小了
    assert "已压缩" in seen[3].data.text


def test_compact_failure_still_clears_the_status(monkeypatch):
    """摘要不合格（放弃压缩）也要摘掉忙碌态，否则前端一直显示「正在压缩」。"""
    agent = _over_threshold_agent(monkeypatch, "不合格的摘要")
    seen: list = []
    agent.events.subscribe(seen.append)

    assert asyncio.run(agent.compact()) is False

    # 放弃压缩：也要发终止事件（否则前端一直显示"正在压缩"），并说明为什么放弃
    assert [type(env.data).__name__ for env in seen] == [
        "CompactionStarted", "CompactionFailed", "Notice",
    ]
    assert seen[1].data.reason  # 放弃的原因
    assert "放弃本次压缩" in seen[2].data.text


@pytest.mark.parametrize("mode", ["empty"])
def test_compact_without_middle_emits_nothing(monkeypatch, mode):
    """无中段可压：直接返回 False，不发状态事件（没什么可显示的）。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: _SummaryLLM(SUMMARY))
    agent = Agent(session=Session())
    seen: list = []
    agent.events.subscribe(seen.append)

    assert asyncio.run(agent.compact()) is False
    assert seen == []

"""交互桥（agent/interactions.py）：阻塞提问的事件对、id 配对与不嵌套金丝雀。

要证明三件事：
1. **成对**：进发 `PromptStarted`、出发 `PromptFinished`（含异常路径），id 相同，
   结束后 `open_prompts` 里不留残项——否则消费者（标题、面板）永远停在「在等」；
2. **可重叠**：真出现并发/嵌套提问时，先结束的那个不会误清另一个（这是从标量
   计数换成 id 配对要换来的能力）；
3. **我们自己的流程不重叠**（金丝雀）：`max_open` 恒为 1，一旦哪天某条路径引入
   了重叠，这条会失败，由人决定是改成顺序还是合并展示——「同时两处在等」从来
   不是需求。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode import config
from smithcode.agent import Agent
from smithcode.agent.interactions import (
    InteractionBridge,
    PromptFinished,
    PromptRequest,
    PromptStarted,
    activate,
    ask,
    reset,
)
from smithcode.session import Session


@pytest.fixture(autouse=True)
def allow_prompting(monkeypatch):
    """pytest 下 stdin 非 TTY：显式放行交互确认，否则权限确认会 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


class Recorder:
    """事件记录器（当作 emit 回调）。"""

    def __init__(self):
        self.events: list = []

    def __call__(self, event) -> None:
        self.events.append(event)

    def of(self, kind: type) -> list:
        return [event for event in self.events if isinstance(event, kind)]


def _bridge():
    recorder = Recorder()
    return InteractionBridge(recorder), recorder


# ---------- 成对与收口 ----------


def test_request_pairs_started_and_finished_with_same_id():
    bridge, recorder = _bridge()

    answer = bridge.request(PromptRequest(kind="permission", title="允许执行 x?"), lambda: "y")

    assert answer == "y"
    started, finished = recorder.of(PromptStarted), recorder.of(PromptFinished)
    assert len(started) == len(finished) == 1
    assert started[0].id == finished[0].id
    assert (started[0].kind, started[0].title) == ("permission", "允许执行 x?")
    assert (finished[0].outcome, finished[0].value) == ("answered", "y")
    assert bridge.open_prompts == {}


def test_prompt_is_open_while_the_port_runs():
    """提问进行中必须能看到它在 open 里——消费者据此判定「还在不在等」。"""
    bridge, _recorder = _bridge()
    observed: list[dict] = []

    bridge.request(
        PromptRequest(kind="ask_user", title="选哪个?"),
        lambda: observed.append(dict(bridge.open_prompts)) or "一",
    )

    assert len(observed[0]) == 1
    assert next(iter(observed[0].values())).title == "选哪个?"
    assert bridge.open_prompts == {}


def test_outcome_of_maps_the_raw_answer():
    bridge, recorder = _bridge()

    bridge.request(
        PromptRequest(kind="ask_user", title="选哪个?"),
        lambda: ["", ""],
        outcome_of=lambda values: "cancelled" if all(not v for v in values) else "answered",
    )

    finished = recorder.of(PromptFinished)[0]
    assert finished.outcome == "cancelled"
    assert finished.value is None  # 非字符串答案不进事件（脱敏）


def test_error_path_finishes_and_reraises():
    """端口自己炸了也要收口：否则消费者永远在等一个不会结束的提问。"""
    bridge, recorder = _bridge()

    def boom() -> str:
        raise RuntimeError("面板挂了")

    with pytest.raises(RuntimeError, match="面板挂了"):
        bridge.request(PromptRequest(kind="permission", title="允许执行 x?"), boom)

    finished = recorder.of(PromptFinished)[0]
    assert finished.outcome == "error"
    assert "面板挂了" in (finished.error or "")
    assert bridge.open_prompts == {}


def test_overlapping_prompts_pair_independently():
    """重叠提问：先结束的不会误清另一个（标量计数做不到这件事）。"""
    bridge, recorder = _bridge()
    inner_finished: list[dict] = []

    def run_inner() -> str:
        bridge.request(PromptRequest(kind="confirm", title="内层"), lambda: "内")
        inner_finished.append(dict(bridge.open_prompts))
        return "外"

    bridge.request(PromptRequest(kind="permission", title="外层"), run_inner)

    started = recorder.of(PromptStarted)
    finished = recorder.of(PromptFinished)
    assert len(started) == len(finished) == 2
    assert bridge.max_open == 2
    assert len(inner_finished[0]) == 1  # 内层结束时外层仍在等
    assert started[0].id == finished[1].id  # 外层后结束
    assert {event.id for event in started} == {event.id for event in finished}
    assert bridge.open_prompts == {}


# ---------- 调用点入口（ask） ----------


def test_ask_without_bridge_calls_through_silently():
    """没有活动桥（单测直接调权限引擎 / 命令层）时语义逐字不变、不发事件。"""
    calls: list[int] = []

    assert ask("permission", title="允许执行 x?", run=lambda: calls.append(1) or "y") == "y"
    assert calls == [1]


def test_ask_with_bridge_emits_the_pair():
    bridge, recorder = _bridge()
    token = activate(bridge)
    try:
        assert ask("skill_trust", title="加载项目技能?", run=lambda: "y") == "y"
    finally:
        reset(token)

    assert [event.kind for event in recorder.of(PromptStarted)] == ["skill_trust"]
    assert bridge.open_prompts == {}


def test_ask_emits_events_from_a_worker_thread():
    """预检跑在 `to_thread` 里：上下文会复制过去，所以 worker 线程也发得出事件。"""
    bridge, recorder = _bridge()
    token = activate(bridge)

    async def scenario():
        return await asyncio.to_thread(ask, "permission", title="允许执行 x?", run=lambda: "y")

    try:
        assert asyncio.run(scenario()) == "y"
    finally:
        reset(token)

    assert len(recorder.of(PromptStarted)) == 1
    assert bridge.open_prompts == {}


# ---------- 金丝雀：我们自己的流程不嵌套 ----------


def test_sequential_prompts_never_overlap():
    """顺序提问两次：`max_open` 保持 1（不会因为「上一次还没收口」而堆起来）。"""
    bridge, _recorder = _bridge()
    token = activate(bridge)
    try:
        for _ in range(2):
            ask("permission", title="允许执行 x?", run=lambda: "y")
    finally:
        reset(token)

    assert bridge.max_open == 1


def _outside_workspace(monkeypatch, tmp_path) -> str:
    """建一个工作区与一个区外文件（触发越界访问确认）。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("区外内容", encoding="utf-8")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(workspace))
    return str(outside)


class ScriptedLLM:
    """先请求一次区外读取，拿到结果后收尾。"""

    def __init__(self, path: str):
        self.path = path
        self.calls = 0

    def chat_stream(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield (
                "message",
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": self.path}),
                            },
                        }
                    ],
                },
            )
            return
        yield ("message", {"role": "assistant", "content": "读完了"})


def test_permission_prompt_in_real_run_pairs_and_never_nests(monkeypatch, tmp_path):
    """金丝雀（真实 run）：越界确认成对发事件、kind 正确、从不重叠。

    走的是完整路径：预检（worker 线程里的权限确认）→ 事件 → 执行。这也是
    「提问事件能穿过 to_thread 到达订阅者」的端到端证据。
    """
    outside = _outside_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(outside))
    agent = Agent(session=Session())
    seen: list = []
    agent.subscribe(seen.append)

    result = asyncio.run(agent.run("读一下区外文件"))

    assert result.status == "ok"
    tool_messages = [m for m in agent.session.messages if m.get("role") == "tool"]
    assert any("区外内容" in str(m.get("content", "")) for m in tool_messages)  # 确实读到了
    started = [event for event in seen if isinstance(event, PromptStarted)]
    finished = [event for event in seen if isinstance(event, PromptFinished)]
    assert [event.kind for event in started] == ["outside_access"]
    assert [event.id for event in finished] == [event.id for event in started]
    assert finished[0].outcome == "answered"
    assert agent.interactions.max_open == 1
    assert agent.interactions.open_prompts == {}

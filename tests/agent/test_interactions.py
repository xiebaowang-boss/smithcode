"""询问事件对（`event/asks.py`）：成对、按 id 配对、路径不重叠。

要证明四件事：
1. **成对**：进发 `PromptStarted`、出发 `PromptFinished`（含异常路径），id 相同——
   否则消费者（终端标题、面板）永远停在「在等」；
2. **可重叠**：真出现并发/嵌套提问时，先结束的那个不会误清另一个（这是从标量
   计数换成 id 配对换来的能力）；
3. **我们自己的流程不重叠**（金丝雀）：事件序列里「进来出去」严格交替，一旦哪天
   某条路径引入重叠，这条会失败，由人决定是改成顺序还是合并展示——「同时两处在等」
   从来不是需求；
4. **无总线时静默**：没有接收方（纯单测直接调权限引擎）不该改变判定行为。

不再断言 `open_prompts` / `max_open`：那个跟踪表已删除（运行期无消费方，配对信息
本来就在事件里），所以这里断言的就是**事件本身**。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode import config, frontend
from smithcode.agent import Agent
from smithcode.event import Bus, activate, reset
from smithcode.event.asks import ask
from smithcode.event.catalog import PromptFinished, PromptStarted
from smithcode.event.envelope import Envelope
from smithcode.frontend.console import ConsoleFrontend
from smithcode.session import Session

TIMEOUT = 5.0


def run(coro):
    """跑一个协程；超时判定为挂死（而不是让测试套件卡住）。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


@pytest.fixture(autouse=True)
def allow_prompting(monkeypatch):
    """pytest 下 stdin 非 TTY：显式放行交互确认，否则权限确认会 fail-closed 拒绝。"""
    monkeypatch.setattr("smithcode.permission.engine.confirmations_available", lambda: True)


class Recorder:
    """事件记录器（作为总线订阅者）：按载荷类型取事件。"""

    def __init__(self) -> None:
        self.events: list[Envelope] = []

    def __call__(self, env: Envelope) -> None:
        self.events.append(env)

    def of(self, kind: type) -> list:
        return [env.data for env in self.events if isinstance(env.data, kind)]

    def sequence(self) -> list[str]:
        """事件类型序列（「不重叠」的断言依据）。"""
        return [env.type for env in self.events]


@pytest.fixture
def wired():
    """挂一个总线 + 记录器：返回 (bus, recorder, token)。"""
    bus = Bus(session_id="sess-test")
    recorder = Recorder()
    bus.subscribe(recorder)
    token = activate(bus)
    yield bus, recorder, token
    reset(token)


# ---------- 成对与收口 ----------


def test_ask_pairs_started_and_finished_with_same_id(wired):
    """一次提问：进出各一个事件，id 相同，kind / title / detail 原样带上。"""
    _bus, recorder, _token = wired

    answer = ask("permission", title="允许执行 x?", detail=("细节",), run=lambda: "y")

    assert answer == "y"
    started, finished = recorder.of(PromptStarted), recorder.of(PromptFinished)
    assert len(started) == len(finished) == 1
    assert started[0].id == finished[0].id
    assert (started[0].kind, started[0].title, started[0].detail) == (
        "permission", "允许执行 x?", ("细节",),
    )
    assert (finished[0].outcome, finished[0].value) == ("answered", "y")


def test_outcome_of_maps_the_raw_answer(wired):
    """调用方可用 `outcome_of` 归类原始返回值（ask_user 的「全空 = 取消」）。"""
    _bus, recorder, _token = wired

    ask("ask_user", title="选哪个?", run=lambda: ["", ""],
        outcome_of=lambda values: "cancelled" if all(not v for v in values) else "answered")

    finished = recorder.of(PromptFinished)[0]
    assert finished.outcome == "cancelled"
    assert finished.value is None  # 非字符串答案不进事件（脱敏）


def test_error_path_finishes_and_reraises(wired):
    """`run` 自己炸了也要收口：否则消费者永远在等一个不会结束的提问。"""
    _bus, recorder, _token = wired

    def boom() -> str:
        raise RuntimeError("面板挂了")

    with pytest.raises(RuntimeError, match="面板挂了"):
        ask("permission", title="允许执行 x?", run=boom)

    finished = recorder.of(PromptFinished)[0]
    assert finished.outcome == "error"
    assert "面板挂了" in (finished.error or "")


def test_overlapping_prompts_pair_independently(wired):
    """重叠提问：先结束的不会误清另一个（标量计数做不到这件事）。"""
    _bus, recorder, _token = wired
    inner_finished: list[tuple] = []

    def run_inner() -> str:
        ask("confirm", title="内层", run=lambda: "内")
        # 内层结束时，外层尚未结束：外层 finished 还没发出
        inner_finished.append(tuple(e.type for e in recorder.events))
        return "外"

    ask("permission", title="外层", run=run_inner)

    started, finished = recorder.of(PromptStarted), recorder.of(PromptFinished)
    assert len(started) == len(finished) == 2
    assert started[0].id == finished[1].id  # 外层后结束
    assert started[1].id == finished[0].id  # 内层先结束
    assert {event.id for event in started} == {event.id for event in finished}
    # 内层结束那一刻：两个 started 已发、只有一个 finished
    assert inner_finished[0] == (
        "session.prompt.started", "session.prompt.started", "session.prompt.finished",
    )


# ---------- 调用点入口（ask） ----------


def test_ask_without_bus_calls_through_silently():
    """没有总线（单测直接调权限引擎 / 命令层）时语义逐字不变、不发事件。"""
    calls: list[int] = []

    assert ask("permission", title="允许执行 x?", run=lambda: calls.append(1) or "y") == "y"
    assert calls == [1]


def test_ask_emits_events_from_a_worker_thread(wired):
    """预检跑在 `to_thread` 里：上下文会复制过去，所以 worker 线程也发得出事件。"""
    _bus, recorder, _token = wired

    async def scenario():
        return await asyncio.to_thread(
            ask, "permission", title="允许执行 x?", run=lambda: "y"
        )

    assert run(scenario()) == "y"
    assert len(recorder.of(PromptStarted)) == 1
    assert len(recorder.of(PromptFinished)) == 1


# ---------- 金丝雀：我们自己的流程不嵌套 ----------


def test_sequential_prompts_never_interleave(wired):
    """顺序提问两次：事件序列必须是「进来出去、进来出去」，不重叠。"""
    _bus, recorder, _token = wired

    for _ in range(2):
        ask("permission", title="允许执行 x?", run=lambda: "y")

    assert recorder.sequence() == [
        "session.prompt.started", "session.prompt.finished",
        "session.prompt.started", "session.prompt.finished",
    ]


# ---------- 真实 run 的端到端证据 ----------


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
    """端到端：越界确认成对发事件、kind 正确、从不重叠。

    走完整路径：预检（worker 线程里的权限确认）→ 事件 → 执行。这也是「提问事件能
    穿过 to_thread 到达订阅者」的证据。装配方式与生产一致：
    `frontend.attach(agent.events, ConsoleFrontend())`——终端前端既订阅事件，
    又是接受询问的那一端（读 stdin）。
    """
    outside = _outside_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(outside))
    agent = Agent(session=Session())
    recorder = Recorder()
    attached = frontend.attach(
        agent.events, ConsoleFrontend(), extra_subscribers=(recorder,)
    )
    try:
        result = run(agent.run("读一下区外文件"))
    finally:
        attached.detach()

    assert result.status == "ok"
    tool_messages = [m for m in agent.session.messages if m.get("role") == "tool"]
    assert any("区外内容" in str(m.get("content", "")) for m in tool_messages)  # 确实读到了
    started = recorder.of(PromptStarted)
    finished = recorder.of(PromptFinished)
    assert [event.kind for event in started] == ["outside_access"]
    assert [event.id for event in finished] == [event.id for event in started]
    assert finished[0].outcome == "answered"
    # 不重叠：只看提问事件，严格交替（记录器抓的是全部事件，含回合与工具）
    assert [env.type for env in recorder.events if env.type.startswith("session.prompt")] == [
        "session.prompt.started", "session.prompt.finished",
    ]

"""询问端口（`event/asks.py`）：asked/replied 成对、按 id 配对、失败与取消都收口。

要证明五件事：
1. **成对**：进发 `PromptStarted`、出发 `PromptFinished`（含异常路径），id 相同——
   否则消费者（终端标题、面板）永远停在「在等」；
2. **答案来自同一个 await**：没有"谁登记、谁收答案"的登记表，答案就是
   `await frontend.ask(request)` 的返回值，构造上错配不了；
3. **我们自己的流程不重叠**（金丝雀）：事件序列里「进来出去」严格交替；
4. **取消也收口**：界面收尾取消挂起询问时，等待方拿到 fail-closed 答案
   （拒绝/取消）而不是异常——退出卡死那个故障的根因就在这里；
5. **无端口即接线错误**：静默退化会让"没人可问"看起来像"用户拒绝"。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from smithcode import config, frontend
from smithcode.agent import Agent
from smithcode.event import Bus, activate, reset
from smithcode.event.asks import (
    FAIL_CLOSED,
    AskAnswer,
    AskPort,
    AskRequest,
    has_port,
    require,
)
from smithcode.event.asks import (
    activate as activate_port,
)
from smithcode.event.asks import (
    reset as reset_port,
)
from smithcode.event.catalog import PromptFinished, PromptStarted
from smithcode.event.envelope import Envelope
from smithcode.frontend.console import ConsoleFrontend
from smithcode.session import Session

TIMEOUT = 5.0


def run(coro):
    """跑一个协程；超时判定为挂死（而不是让测试套件卡住）。"""
    return asyncio.run(asyncio.wait_for(coro, timeout=TIMEOUT))


class FakeFrontend:
    """替身前端：记录收到的请求，按脚本作答（或按脚本抛错 / 一直等）。"""

    def __init__(self, answer=None, *, hang: bool = False, boom: str | None = None):
        self.answer = answer or AskAnswer(outcome="answered", value="y")
        self.hang = hang
        self.boom = boom
        self.requests: list[AskRequest] = []

    async def ask(self, request: AskRequest) -> AskAnswer:
        self.requests.append(request)
        if self.boom:
            raise RuntimeError(self.boom)
        if self.hang:
            await asyncio.Event().wait()  # 永远不返回（模拟界面没作答）
        return self.answer


class Recorder:
    """事件记录器（总线订阅者）。"""

    def __init__(self) -> None:
        self.events: list[Envelope] = []

    def __call__(self, env: Envelope) -> None:
        self.events.append(env)

    def of(self, cls) -> list:
        return [env.data for env in self.events if isinstance(env.data, cls)]

    def sequence(self) -> list[str]:
        return [env.type for env in self.events]


class Wired:
    """一次装配的句柄：(端口, 记录器, 替身前端) + 收尾复位。"""

    def __init__(self) -> None:
        self.bus = Bus(session_id="sess-test")
        self.recorder = Recorder()
        self.bus.subscribe(self.recorder)
        self.frontend = FakeFrontend()
        self.port = AskPort(session_id="sess-test")
        self._tokens = (
            activate(self.bus),
            frontend.activate(self.frontend),
            activate_port(self.port),
        )

    def detach(self) -> None:
        bus_token, frontend_token, port_token = self._tokens
        reset_port(port_token)
        frontend.reset(frontend_token)
        reset(bus_token)


@pytest.fixture
def wired():
    """挂一条总线 + 一个端口 + 记录器 + 替身前端；用完复位。"""
    state = Wired()
    yield state
    state.detach()


# ---------- 成对与答案 ----------


def test_ask_pairs_started_and_finished_with_same_id(wired):
    """一次提问：进出各一个事件，id 相同，请求内容原样带上。"""
    port_obj, recorder = wired.port, wired.recorder
    request = AskRequest(kind="permission", title="允许执行 x?", options=("y", "n"),
                         detail=("细节",), payload={"hint": "y / n"})

    answer = run(port_obj.ask(request))

    assert answer.outcome == "answered" and answer.value == "y"
    started, finished = recorder.of(PromptStarted), recorder.of(PromptFinished)
    assert len(started) == len(finished) == 1
    assert started[0].id == finished[0].id
    assert started[0].kind == "permission"
    assert started[0].detail == ("细节",)
    assert started[0].options == ("y", "n")
    assert (finished[0].outcome, finished[0].value) == ("answered", "y")


def test_answer_comes_from_the_frontend_call(wired):
    """答案就是 `await 前端.ask()` 的返回值（没有登记表，构造上错配不了）。"""
    port_obj, frontend_obj = wired.port, wired.frontend
    frontend_obj.answer = AskAnswer(outcome="answered", value="a")

    answer = run(port_obj.ask(AskRequest(kind="permission", title="?")))

    assert answer.value == "a"
    assert len(frontend_obj.requests) == 1
    assert frontend_obj.requests[0].kind == "permission"


def test_form_answers_are_passed_through(wired):
    """表单类提问（ask_user）：答案按题对齐回传。"""
    port_obj, frontend_obj = wired.port, wired.frontend
    frontend_obj.answer = AskAnswer(outcome="answered", values=("一", "二"))

    questions = [{"question": "一?"}, {"question": "二?"}]
    answer = run(port_obj.ask_user_questions(questions))

    assert answer.values == ("一", "二")
    assert frontend_obj.requests[0].kind == "ask_user"
    assert frontend_obj.requests[0].payload["questions"] == tuple(questions)


def test_error_path_finishes_and_reraises(wired):
    """前端自己炸了也要收口：否则消费者永远在等一个不会结束的提问。"""
    port_obj, recorder, frontend_obj = wired.port, wired.recorder, wired.frontend
    frontend_obj.boom = "面板挂了"

    with pytest.raises(RuntimeError, match="面板挂了"):
        run(port_obj.ask(AskRequest(kind="permission", title="?")))

    finished = recorder.of(PromptFinished)
    assert len(finished) == 1
    assert finished[0].outcome == "error"
    assert "面板挂了" in (finished[0].error or "")


# ---------- 取消（收尾语义） ----------


def test_cancel_in_flight_lets_the_waiter_finish_fail_closed(wired):
    """界面收尾取消挂起询问：等待方拿到 fail-closed 答案，事件成对收口。

    这是退出卡死那个故障的根因所在：等待方必须被放行，且**不能**以异常炸给
    上层（否则这一轮的会话历史会停在半截）。
    """
    port_obj, recorder, frontend_obj = wired.port, wired.recorder, wired.frontend
    frontend_obj.hang = True

    async def scenario():
        task = asyncio.ensure_future(port_obj.ask(AskRequest(kind="permission", title="?")))
        for _ in range(50):  # 等前端真的被问到（ask 内部先起任务再 await）
            await asyncio.sleep(0)
            if frontend_obj.requests:
                break
        assert port_obj.cancel_in_flight() == 1
        return await task

    answer = run(scenario())

    assert answer is FAIL_CLOSED  # 按取消/拒绝收口
    assert frontend_obj.requests  # 前端确实被问过
    assert len(recorder.of(PromptFinished)) == 1  # 事件成对收口


def test_cancel_with_nothing_pending_is_a_noop(wired):
    """没有挂起询问时取消是空操作（收尾路径会无条件调用它）。"""
    assert wired.port.cancel_in_flight() == 0


# ---------- 调用点入口与不重叠 ----------


def test_require_without_port_reports_the_wiring_bug():
    """没有端口 = 接线错误：静默退化会让"没人可问"看起来像"用户拒绝"。"""
    token = activate_port(None)  # 显式断开（本文件其它用例会挂端口）
    try:
        assert has_port() is False
        with pytest.raises(RuntimeError, match="询问端口"):
            require()
    finally:
        reset_port(token)


def test_sequential_prompts_never_interleave(wired):
    """金丝雀：顺序提问两次，事件序列必须「进来出去、进来出去」，不重叠。"""
    port_obj, recorder = wired.port, wired.recorder

    for _ in range(2):
        run(port_obj.ask(AskRequest(kind="permission", title="?")))

    assert recorder.sequence() == [
        "session.prompt.started", "session.prompt.finished",
        "session.prompt.started", "session.prompt.finished",
    ]


def test_ask_sync_from_a_worker_thread(wired):
    """命令层（同步上下文）用 `ask_sync`：借运行中的循环投回协程，结果拿得到。"""
    port_obj, recorder = wired.port, wired.recorder

    async def scenario():
        port_obj.bind_loop(asyncio.get_running_loop())
        return await asyncio.to_thread(
            port_obj.ask_sync, AskRequest(kind="permission", title="?")
        )

    answer = run(scenario())

    assert answer.value == "y"
    assert len(recorder.of(PromptStarted)) == 1
    assert len(recorder.of(PromptFinished)) == 1


def test_ask_sync_from_the_loop_thread_is_refused(wired):
    """在事件循环线程上同步等会自锁——直接报错，而不是挂死。"""
    port_obj = wired.port

    async def scenario():
        port_obj.bind_loop(asyncio.get_running_loop())
        with pytest.raises(RuntimeError, match="自锁"):
            port_obj.ask_sync(AskRequest(kind="permission", title="?"))

    run(scenario())


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

    走完整路径：预检（**在事件循环上**等前端作答）→ 事件 → 执行。装配方式与生产
    一致：`frontend.attach(agent.events, ConsoleFrontend())`——终端前端既订阅事件，
    又是接受询问的那一端（读 stdin）。
    """
    outside = _outside_workspace(monkeypatch, tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(outside))
    monkeypatch.setattr(
        "smithcode.permission.engine.confirmations_available", lambda: True
    )
    agent = Agent(session=Session())
    recorder = Recorder()
    attached = frontend.attach(agent.events, ConsoleFrontend(),
                               extra_subscribers=(recorder,))
    try:
        result = asyncio.run(asyncio.wait_for(agent.run("读一下区外文件"), timeout=TIMEOUT))
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


def test_agent_cancels_pending_asks_on_close(monkeypatch):
    """`Agent.close()` 放行挂起提问：进程退出前不能留下"永远在等"的一方。"""
    agent = Agent(session=Session())
    assert agent.cancel_pending_asks() == 0  # 空操作安全

    async def scenario():
        agent.asks.bind_loop(asyncio.get_running_loop())
        frontend_obj = FakeFrontend(hang=True)
        token = frontend.activate(frontend_obj)
        port_token = activate_port(agent.asks)
        try:
            task = asyncio.ensure_future(
                agent.asks.ask(AskRequest(kind="permission", title="?"))
            )
            await asyncio.sleep(0)
            agent.close()
            return await task
        finally:
            reset_port(port_token)
            frontend.reset(token)

    assert run(scenario()) is FAIL_CLOSED



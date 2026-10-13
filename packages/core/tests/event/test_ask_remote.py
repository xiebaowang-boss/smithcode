"""asked→replied 跨进程路径测试：登记表 + 远端作答 + 幂等 + 收尾。

进程内路径（直调前端）由既有测试覆盖（`test_interactions.py` 等），这里只锁
跨进程新增的语义：挂上 remote 后 `ask()` 不再碰前端、等 `reply()` 唤醒；
迟到/重复作答被忽略；取消按 fail-closed 收口；事件照常配对。
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from smithcode.event import asks as asks_mod
from smithcode.event.asks import AskAnswer, AskPort, AskRequest
from smithcode.event.bus import Bus
from smithcode.event.bus import activate as activate_bus
from smithcode.event.bus import reset as reset_bus
from smithcode.event.catalog import PromptFinished, PromptStarted


@pytest.fixture
def port():
    """挂了 remote 的端口 + 干净总线（事件配对断言用）。

    端口自己的 `session_id` 必须与总线一致，否则 `PromptStarted/Finished`
    会带端口的 id 而测试按总线过滤——生产环境两者由 `Agent` 统一装配。
    """
    bus = Bus(session_id="sess-test")
    bus_token = activate_bus(bus)
    seen: list = []
    bus.subscribe(seen.append)
    p = AskPort(session_id="sess-test")
    p.set_remote_asker(object())  # 非 None 即走跨进程路径（内容不重要）
    yield p, seen
    p.set_remote_asker(None)
    reset_bus(bus_token)


def _request() -> AskRequest:
    return AskRequest(kind="permission", title="允许?", options=("y", "n"))


def _run(coro):
    return asyncio.run(coro)


def test_remote_ask_resolves_on_reply(port):
    """挂 remote 后：`ask()` 等 `reply()` 唤醒，事件配对，登记表清空。"""
    p, seen = port

    async def scenario():
        task = asyncio.ensure_future(p.ask(_request()))
        await asyncio.sleep(0)  # 让 ask() 登记进 pending
        assert len(p.pending) == 1
        prompt_id = p.pending.pending_ids()[0]
        assert p.reply(prompt_id, AskAnswer(outcome="answered", value="y")) is True
        return await task, prompt_id

    answer, prompt_id = _run(scenario())
    assert answer.value == "y"
    assert len(p.pending) == 0
    kinds = [env.type for env in seen]
    assert "session.prompt.started" in kinds and "session.prompt.finished" in kinds
    started = [env for env in seen if isinstance(env.data, PromptStarted)]
    finished = [env for env in seen if isinstance(env.data, PromptFinished)]
    assert len(started) == 1 and len(finished) == 1
    assert started[0].data.id == finished[0].data.id == prompt_id
    assert finished[0].data.value == "y"


def test_duplicate_and_late_replies_are_ignored(port):
    """重复/迟到作答幂等：第一次生效，后续返回 False 且不改变答案。"""
    p, _ = port

    async def scenario():
        task = asyncio.ensure_future(p.ask(_request()))
        await asyncio.sleep(0)
        prompt_id = p.pending.pending_ids()[0]
        first = p.reply(prompt_id, AskAnswer(outcome="answered", value="y"))
        second = p.reply(prompt_id, AskAnswer(outcome="answered", value="n"))
        answer = await task
        late = p.reply(prompt_id, AskAnswer(outcome="answered", value="n"))
        return first, second, answer, late

    first, second, answer, late = _run(scenario())
    assert first is True
    assert second is False  # 收口后（future done）重复作答被忽略
    assert late is False  # settle 后迟到作答被忽略
    assert answer.value == "y"


def test_cancel_in_flight_settles_remote_waiters_fail_closed(port):
    """收尾取消：等 reply 的 `ask()` 被放行，答案 fail-closed，事件成对。"""
    p, seen = port

    async def scenario():
        task = asyncio.ensure_future(p.ask(_request()))
        await asyncio.sleep(0)
        assert len(p.pending) == 1
        assert p.cancel_in_flight() == 1
        return await task

    answer = _run(scenario())
    assert answer.outcome == "cancelled"
    assert len(p.pending) == 0
    finished = [env for env in seen if isinstance(env.data, PromptFinished)]
    assert len(finished) == 1 and finished[0].data.outcome == "cancelled"


def test_ask_user_values_survive_the_remote_round_trip(port):
    """`ask_user` 多题答案经远端往返不丢（`values` 进 `PromptFinished`）。"""
    p, seen = port

    async def scenario():
        task = asyncio.ensure_future(p.ask(AskRequest(kind="ask_user", title="选")))
        await asyncio.sleep(0)
        prompt_id = p.pending.pending_ids()[0]
        p.reply(prompt_id, AskAnswer(outcome="answered", values=("一", "二")))
        return await task

    answer = _run(scenario())
    assert answer.values == ("一", "二")
    finished = next(env for env in seen if isinstance(env.data, PromptFinished))
    assert tuple(finished.data.values) == ("一", "二")


def test_pending_snapshot_is_json_serializable(port):
    """挂起快照可 JSON 化（`GET pending` / SSE 重连补发用）。"""
    import json

    from smithcode.event.envelope import payload_to_dict

    p, _ = port

    async def scenario():
        task = asyncio.ensure_future(p.ask(_request()))
        await asyncio.sleep(0)
        snapshot = p.pending_snapshot()
        assert len(snapshot) == 1
        json.dumps(payload_to_dict(snapshot[0]))  # 快照必须可 JSON 化
        p.cancel_in_flight()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    _run(scenario())


def test_local_path_is_unchanged_without_remote():
    """没挂 remote 时走原进程内路径（回归：不挂 remote = 旧行为逐字不变）。"""
    from smithcode import frontend

    class Asker:
        async def ask(self, request):
            return AskAnswer(outcome="answered", value="y")

    async def scenario():
        p = AskPort(session_id="s")
        port_token = asks_mod.activate(p)
        frontend_token = frontend.activate(Asker())
        try:
            assert p.remote is None
            return await p.ask(_request())
        finally:
            frontend.reset(frontend_token)
            asks_mod.reset(port_token)

    assert _run(scenario()).value == "y"

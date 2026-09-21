"""守卫：同进程**两个会话并发**时互不串味（阶段 D 的验收，四条探针）。

改造前这些状态是 `config` / 模块里的进程级可变值，"一个进程一个会话"是隐含前提。
多客户端/多会话一旦成立，它们会彼此覆盖——而且**不会报错**，只会在某个场景下
悄悄算错（比如把别人放行的目录删掉、把旧会话的目标显示给新会话）。

四条探针各自锁住一处：
1. 会话状态（goal / plan / skills）按上下文隔离；
2. 王界信任目录（会话沙箱）不跨会话，`/new` 只清自己那份；
3. 单次调用临时放行（`widen`）不跨会话，且**交错进出不删错条目**（旧实现的
   `del _WIDENED_ROOTS[-n:]` 会删掉别人刚放行的目录）；
4. 事件总线与询问端口各自独立（事件只到自己的订阅者）。
"""

from __future__ import annotations

import asyncio

from smithcode import config, goal, plan, sandbox
from smithcode.agent import Agent
from smithcode.event.asks import AskAnswer
from smithcode.event.asks import activate as activate_port
from smithcode.event.asks import reset as reset_port
from smithcode.event.catalog import Notice
from smithcode.session import Session
from smithcode.skills import state as skills_state


def _agent(monkeypatch, text="回复") -> Agent:
    class FakeLLM:
        def chat_stream(self, messages, tools=None):
            yield ("message", {"role": "assistant", "content": text})

    monkeypatch.setattr("smithcode.agent.LLMClient", FakeLLM)
    return Agent(session=Session())


def _run_together(*coros):
    """并发跑多个协程（各自独立上下文，模拟同进程两个会话同时工作）。"""
    async def scenario():
        return await asyncio.gather(*coros)

    return asyncio.run(scenario())


# ---------- 1. 会话状态按上下文隔离 ----------


def test_session_state_is_isolated_between_concurrent_sessions(monkeypatch):
    """两个会话并发：goal / plan / skills 各改各的，互不覆盖。"""
    a, b = _agent(monkeypatch, "A 的回复"), _agent(monkeypatch, "B 的回复")
    owner_a, owner_b = a.session_owner, b.session_owner

    async def session_a():
        token = _bind(owner_a)
        try:
            goal.set("目标 A", max_turns=3)
            plan.current().replace([{"title": "A 的步骤", "status": "in_progress"}])
            await asyncio.sleep(0.01)  # 让 B 插进来
            return goal.current().objective, [i["title"] for i in plan.current().items]
        finally:
            _unbind(token)

    async def session_b():
        await asyncio.sleep(0)  # 确保与 A 交错
        token = _bind(owner_b)
        try:
            goal.set("目标 B", max_turns=3)
            plan.current().replace([{"title": "B 的步骤", "status": "in_progress"}])
            await asyncio.sleep(0.01)
            return goal.current().objective, [i["title"] for i in plan.current().items]
        finally:
            _unbind(token)

    got_a, got_b = _run_together(session_a(), session_b())

    assert got_a == ("目标 A", ["A 的步骤"])
    assert got_b == ("目标 B", ["B 的步骤"])
    # 进程默认实例没有被任何一方的会话状态污染（会话状态各在各的对象里）
    goal.bind(None)
    plan.bind(None)
    skills_state.bind(None)
    assert not goal.is_active()
    assert plan.current().items == []


def _bind(owner):
    """把某个会话的状态实例挂到当前上下文（复用 AgentSession 的绑定入口）。"""
    goal.bind(owner.goal_state)
    plan.bind(owner.plan_state)
    skills_state.bind(owner.skills_state)
    return owner


def _unbind(owner) -> None:
    goal.bind(None)
    plan.bind(None)
    skills_state.bind(None)


# ---------- 2. 会话沙箱：信任目录不跨会话 ----------


def test_session_trust_roots_do_not_leak_across_sessions(monkeypatch, tmp_path):
    """A 的越界信任目录不泄漏给 B；A 的 `/new` 也不清 B 的那份。"""
    a, b = _agent(monkeypatch), _agent(monkeypatch)
    shared = tmp_path / "shared"
    shared.mkdir()

    a.roots.session_extra.append(shared)
    b.roots.session_extra.append(tmp_path / "b-only")

    assert shared in a.roots.allowed()
    assert shared not in b.roots.allowed()  # 不串味
    assert tmp_path / "b-only" in b.roots.allowed()

    a.new_session()  # A 换会话：只清 A 自己那份
    assert a.roots.session_extra == []
    assert b.roots.session_extra == [tmp_path / "b-only"]


# ---------- 3. 临时放行：交错进出不删错条目 ----------


def test_widen_roots_are_context_scoped_and_do_not_clobber(monkeypatch, tmp_path):
    """两个会话各自的"仅本次"放行互不可见；交错退出也不会删掉对方刚放行的目录。

    旧实现是一个进程级列表 + `del _WIDENED_ROOTS[-len(added):]`：交错时后来者
    的条目会把先来者的删掉——放行范围凭空消失，沙箱判定随之漂移。
    """
    a, b = _agent(monkeypatch), _agent(monkeypatch)
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"

    async def session_a():
        token = sandbox.activate(a.roots)
        try:
            with config.widen_roots([dir_a]):
                await asyncio.sleep(0.02)  # B 在这期间进出
                return [str(p) for p in sandbox.current().allowed()]
        finally:
            sandbox.reset(token)

    async def session_b():
        token = sandbox.activate(b.roots)
        try:
            with config.widen_roots([dir_b]):
                await asyncio.sleep(0.01)
            # B 已退出自己的 widen：A 的放行必须还在
            return [str(p) for p in sandbox.current().allowed()]
        finally:
            sandbox.reset(token)

    seen_a, seen_b = _run_together(session_a(), session_b())

    assert str(dir_a.resolve()) in seen_a  # A 自己的放行在
    assert str(dir_b.resolve()) not in seen_a  # B 的放行不进 A
    assert str(dir_b.resolve()) not in seen_b  # B 已退出，放行收回
    assert str(dir_a.resolve()) not in seen_b  # 更没有串味
    # 收尾后两边都干净（条目没有互相删错、也没有残留）
    assert not a.roots.allowed()[1:] or str(dir_a.resolve()) not in a.roots.allowed()


# ---------- 4. 总线与询问端口各自独立 ----------


def test_buses_and_ask_ports_are_per_session(monkeypatch):
    """事件只到自己的订阅者；两个会话的询问端口互不接管。"""
    a, b = _agent(monkeypatch), _agent(monkeypatch)
    seen_a: list = []
    seen_b: list = []
    a.events.subscribe(seen_a.append)
    b.events.subscribe(seen_b.append)

    a.events.publish(Notice("只给 A"))
    b.events.publish(Notice("只给 B"))

    assert [env.data.text for env in seen_a] == ["只给 A"]
    assert [env.data.text for env in seen_b] == ["只给 B"]
    assert a.events.session_id != b.events.session_id  # 会话标识各一份

    # 询问端口：各等各的前端作答，互不干扰
    class Asker:
        def __init__(self, value):
            self.value = value

        async def ask(self, request):
            return AskAnswer(outcome="answered", value=self.value)

    from smithcode import frontend

    async def ask_with(agent, answer_value):
        token = activate_port(agent.asks)
        frontend_token = frontend.activate(Asker(answer_value))
        try:
            return await agent.asks.ask(_request())
        finally:
            frontend.reset(frontend_token)
            reset_port(token)

    got_a, got_b = _run_together(ask_with(a, "A 答的"), ask_with(b, "B 答的"))

    assert got_a.value == "A 答的"
    assert got_b.value == "B 答的"
    assert a.asks.cancel_in_flight() == 0  # 都收口了，没有挂起
    assert b.asks.cancel_in_flight() == 0


def _request():
    from smithcode.event.asks import AskRequest

    return AskRequest(kind="permission", title="允许?", options=("y", "n"))


# ---------- 附：并发持有的沙箱各算各的 ----------


def test_concurrent_sandboxes_keep_their_own_roots(monkeypatch, tmp_path):
    """两个会话同时持有沙箱：各自看到的授权目录是自己那一份。"""
    a, b = _agent(monkeypatch), _agent(monkeypatch)
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    a.roots.session_extra.append(dir_a)
    b.roots.session_extra.append(dir_b)

    async def seen_roots(agent):
        token = sandbox.activate(agent.roots)  # 与 run 的挂载语义一致
        try:
            await asyncio.sleep(0.005)  # 交错
            return [str(p) for p in sandbox.current().allowed()]
        finally:
            sandbox.reset(token)

    got_a, got_b = _run_together(seen_roots(a), seen_roots(b))

    assert str(dir_a) in got_a and str(dir_b) not in got_a
    assert str(dir_b) in got_b and str(dir_a) not in got_b


# ---------- 附：run 真的会挂上本会话的沙箱 ----------


def test_run_activates_the_session_sandbox(monkeypatch, tmp_path):
    """`Agent.run` 期间沙箱是本会话的那份（挂载点写在 run 里，见 roots_token）。"""
    agent = _agent(monkeypatch)
    extra = tmp_path / "extra"
    agent.roots.session_extra.append(extra)
    seen: dict = {}

    async def probe():
        seen["allowed"] = [str(p) for p in sandbox.current().allowed()]

    original = agent._hook_prepare_next_turn

    async def spy(turn):  # 钩子签名见 hooks.py
        await probe()
        return await original(turn)

    monkeypatch.setattr(agent, "_hook_prepare_next_turn", spy)
    asyncio.run(agent.run("看一下"))

    assert str(extra) in seen["allowed"]

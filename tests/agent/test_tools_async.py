"""工具两种签名都收：同步工具下放线程，异步工具在事件循环上 await。

存在的意义（方案 §7(2)）：让**一个**异步工具（例如将来异步化的 MCP 工具）可以
直接注册使用，而不必为了它把全部内置工具改一遍。
"""

from __future__ import annotations

import asyncio
import json
import threading

from smithcode.agent import Agent
from smithcode.session import Session


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)

    def chat_stream(self, messages, tools=None):
        msg = self.script.pop(0) if self.script else {"role": "assistant", "content": "（用尽）"}
        yield ("message", msg)


def _tool(name, args=None, call_id="1"):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args or {})},
            }
        ],
    }


def _text(content):
    return {"role": "assistant", "content": content}


def _register(monkeypatch, name: str, func) -> None:
    """注册工具并放行权限（聚焦签名兼容，避开真实权限规则）。"""
    from smithcode.permission import Permission
    from smithcode.tools import FUNCTIONS

    monkeypatch.setitem(FUNCTIONS, name, func)
    async def _allow(self, *args, **kwargs):  # 权限检查是协程（提问要 await 前端）
        return True

    monkeypatch.setattr(Permission, "check", _allow)
    monkeypatch.setattr(Permission, "check_paths", _allow)


def _agent(monkeypatch, script) -> Agent:
    monkeypatch.setattr("smithcode.agent.LLMClient", lambda: ScriptedLLM(script))
    return Agent(session=Session())


def _tool_messages(agent) -> list[str]:
    return [m["content"] for m in agent.session.messages if m.get("role") == "tool"]


def test_async_tool_is_awaited(monkeypatch):
    """`async def` 工具：返回值经 await 后进入会话历史（而不是被 str() 成协程）。"""
    ran: list[str] = []

    async def fake_async(path=None):
        await asyncio.sleep(0)  # 真正让出一次：证明确实在循环上 await 了
        ran.append(path or "")
        return f"异步结果 {path}"

    _register(monkeypatch, "fake_async", fake_async)
    agent = _agent(monkeypatch, [_tool("fake_async", {"path": "a"}), _text("结束")])

    result = asyncio.run(agent.run("跑异步工具"))

    assert result.status == "ok"
    assert ran == ["a"]
    assert _tool_messages(agent) == ["异步结果 a"]


def test_sync_tool_still_runs_off_the_loop_thread(monkeypatch):
    """同步工具仍在 worker 线程执行（不能因为「两种签名都收」就把它挪回循环）。"""
    seen_thread: list[str] = []

    def fake_sync(path=None):
        seen_thread.append(threading.current_thread().name)
        return "同步结果"

    _register(monkeypatch, "fake_sync", fake_sync)
    agent = _agent(monkeypatch, [_tool("fake_sync", {"path": "a"}), _text("结束")])

    asyncio.run(agent.run("跑同步工具"))

    assert _tool_messages(agent) == ["同步结果"]
    # 主线程名不会是 asyncio 的默认线程池名；这里断言它确实不是发起方那条线程
    assert seen_thread and seen_thread[0] != threading.main_thread().name


def test_async_tool_error_becomes_a_result_not_a_crash(monkeypatch):
    """异步工具抛异常：与其他工具一致，转成结果文本回传（循环不中断）。"""

    async def boom(path=None):
        raise RuntimeError("异步工具炸了")

    _register(monkeypatch, "fake_async", boom)
    agent = _agent(monkeypatch, [_tool("fake_async", {}), _text("结束")])

    result = asyncio.run(agent.run("跑异步工具"))

    assert result.status == "ok"
    assert "异步工具炸了" in _tool_messages(agent)[0]

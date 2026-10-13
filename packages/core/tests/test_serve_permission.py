"""serve 端点的回归测试：permission 切换 + list_online 扩展字段 + 跨进程确认链路。"""

from __future__ import annotations

import json
import time


class _WriteLLM:
    """第一轮请求写文件（内置规则 ask，必须确认），第二轮收尾。"""

    accepts_turn_params = False

    def __init__(self) -> None:
        self.calls = 0

    def chat_stream(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            yield ("message", {
                "role": "assistant", "content": "",
                "tool_calls": [{
                    "id": "c1", "type": "function",
                    "function": {"name": "write_file",
                                 "arguments": json.dumps({"path": "hello.txt",
                                                          "content": "hi"})},
                }],
            })
            return
        yield ("message", {"role": "assistant", "content": "写好了"})


def test_permission_mode_switch(served, serve_call):
    """`POST permission` 切模式：合法值生效，非法值 400 且不动。"""
    base, token, state = served
    _, body = serve_call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    assert state.get(sid).agent.permission.mode == "smith"

    status, body = serve_call(base, token, "POST", f"/sessions/{sid}/permission",
                              {"mode": "auto"})
    assert status == 200 and body["mode"] == "auto"
    assert state.get(sid).agent.permission.mode == "auto"

    status, _ = serve_call(base, token, "POST", f"/sessions/{sid}/permission",
                           {"mode": "oops"})
    assert status == 400
    assert state.get(sid).agent.permission.mode == "auto"  # 非法值不动

    status, body = serve_call(base, token, "GET", "/sessions")
    entry = next(s for s in body["sessions"] if s["id"] == sid)
    assert entry["permission"] == "auto"
    assert "model" in entry and "effort" in entry


def _start_write_turn(served, serve_call, monkeypatch, tmp_path):
    """起一个会话并异步开跑"写文件"那一轮：返回 (sid, entry, future, seen)。"""
    monkeypatch.setattr("smithcode.agent.LLMClient", _WriteLLM)
    base, token, state = served
    _, body = serve_call(base, token, "POST", "/sessions", {})
    sid = body["id"]
    entry = state.get(sid)
    seen: list[str] = []
    entry.bus.subscribe(lambda env: seen.append(env.type))
    future = entry.run_in_loop(entry.agent.prompt("建个文件"))
    return base, token, sid, entry, future, seen


def _wait_pending(serve_call, base, token, sid):
    """等询问挂起（门禁放行 → 登记 pending）。"""
    for _ in range(100):
        _, body = serve_call(base, token, "GET", f"/sessions/{sid}/pending")
        if body.get("pending"):
            return body["pending"][0]
        time.sleep(0.05)
    return None


def test_permission_ask_surfaces_and_allows(served, serve_call, monkeypatch, tmp_path):
    """桌面端场景：serve 下权限确认走完整链路（三处此前断掉的接线）。

    1. 门禁不再按本进程 stdin 判死——sidecar 的 stdin 是 /dev/null，而 serve 挂了
       跨进程作答者（`POST reply`），有人可问就必须问出来；
    2. `PromptStarted` 必须到达会话总线：SSE 桥订阅的就是它，到不了 = 前端永远
       不知道有询问在等（这就是"我没收到请求"的第二半根因）；
    3. `GET pending` → `POST reply` 唤醒后，工具按用户的选择真正执行。
    """
    base, token, sid, entry, future, seen = _start_write_turn(
        served, serve_call, monkeypatch, tmp_path)

    pending = _wait_pending(serve_call, base, token, sid)
    assert pending is not None, "挂了跨进程作答者就该问出来，不能被 stdin 门禁误杀"
    assert pending["kind"] == "permission"
    assert "session.prompt.started" in seen, (
        "PromptStarted 没到会话总线：SSE 收不到，前端弹不出确认框"
    )

    status, body = serve_call(base, token, "POST",
                              f"/sessions/{sid}/prompts/{pending['id']}/reply",
                              {"outcome": "answered", "value": "y"})
    assert status == 200 and body["ok"] is True

    result = future.result(timeout=30)
    assert result.status == "ok"
    assert (tmp_path / "hello.txt").read_text(encoding="utf-8") == "hi"
    assert any(m.get("role") == "tool" for m in entry.agent.session.messages)  # 工具真跑了
    assert "session.prompt.finished" in seen  # 事件成对收口


def test_permission_ask_denied_by_user_stops_task(served, serve_call, monkeypatch,
                                                  tmp_path):
    """用户在弹窗里点"拒绝"：任务按 denied 收口、文件不落盘。"""
    base, token, sid, entry, future, seen = _start_write_turn(
        served, serve_call, monkeypatch, tmp_path)

    pending = _wait_pending(serve_call, base, token, sid)
    assert pending is not None
    status, body = serve_call(base, token, "POST",
                              f"/sessions/{sid}/prompts/{pending['id']}/reply",
                              {"outcome": "answered", "value": "n"})
    assert status == 200 and body["ok"] is True

    result = future.result(timeout=30)
    assert result.status == "denied"
    assert not (tmp_path / "hello.txt").exists()
    assert "session.prompt.finished" in seen
    tool_msgs = [m for m in entry.agent.session.messages if m.get("role") == "tool"]
    assert any("拒绝" in str(m.get("content")) for m in tool_msgs)


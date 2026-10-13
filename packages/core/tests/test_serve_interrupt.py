"""serve 的 interrupt 路由：HTTP 层点停止要能停掉正在跑的轮次。

`test_serve.py` 的 FakeLLM 一轮秒回，测不到「跑一半点停止」；这里用长流假
LLM（一直吐、永不结束）复现桌面端停止按钮的真实场景。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest


def _call(base, token, method, path, body=None):
    data = json.dumps(body or {}).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


@pytest.fixture
def served_long(monkeypatch, tmp_path):
    """起真 HTTP serve，但假 LLM 一直吐流（任务不会自己结束）。"""
    from http.server import ThreadingHTTPServer

    import smithcode.serve as serve_mod
    from smithcode import config

    monkeypatch.setenv("SMITHCODE_HOME", str(tmp_path / "home"))

    class LongLLM:
        accepts_turn_params = False

        def chat_stream(self, messages, tools=None, **kwargs):
            for i in range(10_000):
                yield ("message", {"role": "assistant", "content": f"第 {i} 段"})
                time.sleep(0.02)

    monkeypatch.setattr("smithcode.agent.LLMClient", LongLLM)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr("smithcode.sessions.paths.config.WORKSPACE_ROOT", str(tmp_path))

    state = serve_mod.ServeState(auth_token="test-token")
    state.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), serve_mod._Handler)
    server.state = state
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, "test-token", state
    server.shutdown()
    state.close()


def _wait_until(cond, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_interrupt_stops_running_turn(served_long):
    """流消费中 POST interrupt：轮次在数秒内以中断收场、busy 复位。"""
    base, token, state = served_long
    _, body = _call(base, token, "POST", "/sessions", {})
    sid = body["id"]

    threading.Thread(
        target=lambda: _call(
            base, token, "POST", f"/sessions/{sid}/prompt_async", {"text": "跑"}
        ),
        daemon=True,
    ).start()

    entry = state.get(sid)
    assert _wait_until(lambda: entry.agent.busy), "轮次没跑起来"
    time.sleep(0.1)  # 让流先吐几口，进入消费态

    status, body = _call(base, token, "POST", f"/sessions/{sid}/interrupt", {})
    assert status == 200 and body["ok"] is True

    assert _wait_until(lambda: not entry.agent.busy, timeout=5), "interrupt 后任务没停"
    online = _call(base, token, "GET", "/sessions")[1]["sessions"]
    assert [s["busy"] for s in online if s["id"] == sid] == [False]


def test_interrupt_unknown_session_is_404(served_long):
    """会话不存在（已 purge / 乱写 id）：回 404 而不是未处理异常。"""
    base, token, _ = served_long
    status, body = _call(base, token, "POST", "/sessions/deadbeef/interrupt", {})
    assert status == 404
    assert "不存在" in str(body.get("error", ""))

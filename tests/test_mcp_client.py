"""MCP stdio 客户端测试：握手、工具调用、超时、取消、崩溃与坏输出诊断。"""

import sys
import threading
from pathlib import Path

import pytest

from smithcode.cancel import CancellationToken, activate_token
from smithcode.mcp.client import StdioConnection
from smithcode.mcp.errors import McpError

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _connection(mode=None, timeout=10.0, **extra_env):
    env = dict(extra_env)
    if mode:
        env["FAKE_MCP_MODE"] = mode
    return StdioConnection(
        "fake", [sys.executable, str(FIXTURE)], env=env, timeout=timeout
    )


def test_handshake_and_list_tools():
    conn = _connection()
    try:
        info = conn.start()
        assert info["serverInfo"]["name"] == "fake"
        names = [tool["name"] for tool in conn.list_tools()]
        assert names == ["echo", "slow", "fail", "big", "structured"]
        assert conn.alive
    finally:
        conn.close()
    assert not conn.alive


def test_call_tool():
    conn = _connection()
    try:
        conn.start()
        result = conn.call_tool("echo", {"msg": "hi"})
        assert result["content"][0]["text"] == "echo: hi"
    finally:
        conn.close()


def test_call_timeout_then_connection_still_usable():
    conn = _connection()
    try:
        conn.start()
        with pytest.raises(McpError) as excinfo:
            conn.call_tool("slow", {}, timeout=0.3)
        assert "超时" in str(excinfo.value)
        # 迟到响应被忽略，连接仍可继续使用
        assert conn.call_tool("echo", {"msg": "after"})["content"][0]["text"] == "echo: after"
    finally:
        conn.close()


def test_call_interrupted_by_token():
    conn = _connection()
    token = CancellationToken()
    reset = activate_token(token)
    timer = threading.Timer(0.3, token.cancel)
    timer.start()
    try:
        conn.start()
        with pytest.raises(McpError) as excinfo:
            conn.call_tool("slow", {})
        assert "中断" in str(excinfo.value)
    finally:
        timer.cancel()
        reset()
        conn.close()


def test_bad_json_line_is_diagnosed_not_fatal():
    conn = _connection(mode="bad-json")
    try:
        conn.start()
        assert conn.malformed_lines >= 1
        assert "非 JSON" in conn.stderr_tail()
        assert conn.call_tool("echo", {"msg": "ok"})["content"][0]["text"] == "echo: ok"
    finally:
        conn.close()


def test_exit_before_handshake_raises():
    conn = _connection(mode="exit")
    with pytest.raises(McpError) as excinfo:
        conn.start()
    assert "握手失败" in str(excinfo.value)
    conn.close()


def test_missing_command_raises():
    conn = StdioConnection("nope", ["definitely-not-a-real-command-xyz"])
    with pytest.raises(McpError) as excinfo:
        conn.start()
    assert "找不到命令" in str(excinfo.value)


def test_tools_changed_callback():
    fired = threading.Event()

    def on_changed(name):
        assert name == "fake"
        fired.set()

    conn = _connection(mode="list-changed", timeout=5.0)
    conn._on_tools_changed = on_changed
    try:
        conn.start()
        assert fired.wait(3.0), "未收到 tools/list_changed 通知"
    finally:
        conn.close()


def test_close_is_idempotent_and_fails_inflight():
    conn = _connection()
    conn.start()
    conn.close()
    conn.close()
    with pytest.raises(McpError):
        conn.call_tool("echo", {"msg": "x"})


def test_list_tools_empty():
    conn = _connection(mode="no-tools")
    try:
        conn.start()
        assert conn.list_tools() == []
    finally:
        conn.close()

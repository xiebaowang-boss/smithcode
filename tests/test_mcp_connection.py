"""MCP SDK 连接适配层测试：握手、工具调用、超时、取消、断开检测与通知。

连接层现在建立在官方 SDK 之上（`mcp.connection.SdkConnection`），但仍以
同步门面 + 共享 loop 线程对上层服务暴露。这里验证门面语义与传输监控。
"""

import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

from smithcode.cancel import CancellationToken, activate_token
from smithcode.mcp import config as mcp_config
from smithcode.mcp import secrets
from smithcode.mcp.connection import SdkConnection
from smithcode.mcp.errors import McpError
from smithcode.mcp.runtime import AsyncRuntime

FIXTURE = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


def _connection(mode=None, timeout=10.0, on_tools_changed=None, on_closed=None, **extra_env):
    env = dict(extra_env)
    if mode:
        env["FAKE_MCP_MODE"] = mode
    cfg = mcp_config.ServerConfig(
        name="fake", command=[sys.executable, str(FIXTURE)], env=env, timeout=timeout
    )
    resolved = secrets.resolve(cfg)
    return SdkConnection(
        cfg, resolved, AsyncRuntime(),
        on_tools_changed=on_tools_changed, on_closed=on_closed,
    )


def _teardown(conn):
    conn.close()
    conn.runtime.stop()


def test_handshake_and_list_tools():
    conn = _connection()
    try:
        info = conn.start()
        assert info["name"] == "fake"
        names = [tool["name"] for tool in conn.list_tools()]
        assert names == ["echo", "slow", "fail", "big", "structured"]
        assert conn.alive
    finally:
        _teardown(conn)
    assert not conn.alive


def test_call_tool():
    conn = _connection()
    try:
        conn.start()
        result = conn.call_tool("echo", {"msg": "hi"})
        assert result["content"][0]["text"] == "echo: hi"
    finally:
        _teardown(conn)


def test_structured_result_kept():
    conn = _connection()
    try:
        conn.start()
        result = conn.call_tool("structured", {})
        assert result["structuredContent"]["ok"] is True
    finally:
        _teardown(conn)


def test_call_timeout_then_connection_still_usable():
    conn = _connection()
    try:
        conn.start()
        with pytest.raises(McpError) as excinfo:
            conn.call_tool("slow", {}, timeout=0.3)
        assert "超时" in str(excinfo.value)
        assert conn.call_tool("echo", {"msg": "after"})["content"][0]["text"] == "echo: after"
    finally:
        _teardown(conn)


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
        _teardown(conn)


def test_crash_notifies_on_closed():
    closed = threading.Event()
    conn = _connection(mode="crash-tool", on_closed=lambda c: closed.set())
    try:
        conn.start()
        assert conn.alive
        with pytest.raises(McpError):
            conn.call_tool("crash", {})
        assert closed.wait(5.0), "未收到连接断开通知"
        assert not conn.alive
    finally:
        _teardown(conn)


def test_tools_changed_callback():
    fired = threading.Event()
    conn = _connection(mode="list-changed", timeout=5.0,
                       on_tools_changed=lambda c: fired.set())
    try:
        conn.start()
        assert fired.wait(3.0), "未收到 tools/list_changed 通知"
    finally:
        _teardown(conn)


def test_progress_reporter_throttles_by_10_percent(monkeypatch):
    conn = _connection()
    events = []

    from smithcode.event import Bus, activate
    from smithcode.event.catalog import Notice

    bus = Bus(session_id="t")
    bus.subscribe(lambda env: isinstance(env.data, Notice) and events.append(env.data.text))
    _bus_token = activate(bus)
    report = conn._progress_reporter("slow")
    asyncio.run(report(1, 100, None))    # 1% → 上报
    asyncio.run(report(5, 100, None))    # 5% → 节流
    asyncio.run(report(12, 100, "下载中"))  # 12% → 上报
    asyncio.run(report(50, None, None))  # total 未知 → 不展示
    _teardown(conn)
    assert len(events) == 2
    assert "1%" in events[0]
    assert "12%" in events[1] and "下载中" in events[1]


def test_missing_command_raises():
    cfg = mcp_config.ServerConfig(
        name="nope", command=["definitely-not-a-real-command-xyz"], timeout=5.0
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    with pytest.raises(McpError):
        conn.start()
    _teardown(conn)


def test_exit_before_handshake_raises():
    conn = _connection(mode="exit")
    with pytest.raises(McpError):
        conn.start()
    _teardown(conn)


@pytest.mark.skipif(
    os.environ.get("SMITHCODE_MCP_E2E") != "1",
    reason="真实第三方 server（npx + 网络）；设 SMITHCODE_MCP_E2E=1 启用",
)
def test_real_everything_server_smoke():
    """对官方 @modelcontextprotocol/server-everything 的真实冒烟：列工具 + 调用。"""
    cfg = mcp_config.ServerConfig(
        name="everything",
        command=["npx", "-y", "@modelcontextprotocol/server-everything"],
        timeout=120.0,
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    try:
        conn.start()
        names = [tool["name"] for tool in conn.list_tools()]
        assert "echo" in names and "trigger-long-running-operation" in names
        result = conn.call_tool("echo", {"message": "hi"}, timeout=60.0)
        assert "Echo: hi" in result["content"][0]["text"]
        long = conn.call_tool(
            "trigger-long-running-operation", {"duration": 1, "steps": 3}, timeout=60.0
        )
        assert not long.get("isError")
    finally:
        _teardown(conn)


# ---------- Streamable HTTP：起一个真实 SDK server，验证远程传输 ----------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _HttpServer:
    """用 SDK 的 MCPServer + uvicorn 起一个真实 Streamable HTTP 端点。"""

    def __init__(self):
        self.server = MCPServer("fake-http")
        self.seen_headers: dict = {}

        @self.server.tool()
        def echo(msg: str) -> str:
            """回显输入。"""
            return f"echo: {msg}"

        @self.server.tool()
        async def work(ctx: Context, steps: int = 3) -> str:
            """按步上报进度。"""
            for index in range(steps):
                await ctx.report_progress(index + 1, steps, f"step {index + 1}")
            return "work done"

        @self.server.tool()
        async def grow(ctx: Context) -> str:
            """运行时新增工具并广播列表变更。"""
            def extra() -> str:
                """动态加入的工具。"""
                return "extra"

            self.server.add_tool(extra)
            await ctx.notify_tools_changed()
            return "grown"

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        inner = self.server.streamable_http_app()

        async def app(scope, receive, send):
            if scope["type"] == "http":
                for key, value in scope.get("headers", []):
                    self.seen_headers[key.decode("latin-1").lower()] = value.decode("latin-1")
            await inner(scope, receive, send)

        self._uvicorn = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning",
        ))
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)

    def start(self):
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._uvicorn.started:
            time.sleep(0.05)
        assert self._uvicorn.started, "本地 HTTP MCP server 启动失败"

    def stop(self):
        self._uvicorn.should_exit = True
        self._thread.join(5)


def test_http_transport_roundtrip():
    server = _HttpServer()
    server.start()
    cfg = mcp_config.ServerConfig(
        name="remote", type="http", url=server.url, timeout=10.0
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    try:
        conn.start()
        assert conn.alive
        assert "echo" in [tool["name"] for tool in conn.list_tools()]
        result = conn.call_tool("echo", {"msg": "hi"})
        assert result["content"][0]["text"] == "echo: hi"
    finally:
        _teardown(conn)
        server.stop()


def test_http_transport_sends_headers():
    server = _HttpServer()
    server.start()
    cfg = mcp_config.ServerConfig(
        name="remote", type="http", url=server.url, timeout=10.0,
        headers={"Authorization": "Bearer ${MCP_HTTP_TEST_TOKEN}"},
    )
    from smithcode import config as smith_config

    original = smith_config._read_credentials
    smith_config._read_credentials = lambda: {
        "mcp": {"remote": {"MCP_HTTP_TEST_TOKEN": "secret-token"}}
    }
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    try:
        conn.start()
        conn.call_tool("echo", {"msg": "hi"})
        assert server.seen_headers.get("authorization") == "Bearer secret-token"
    finally:
        _teardown(conn)
        server.stop()
        smith_config._read_credentials = original


def test_http_progress_callback(monkeypatch):
    server = _HttpServer()
    server.start()
    events = []

    from smithcode.event import Bus, activate
    from smithcode.event.catalog import Notice

    bus = Bus(session_id="t")
    bus.subscribe(lambda env: isinstance(env.data, Notice) and events.append(env.data.text))
    _bus_token = activate(bus)
    cfg = mcp_config.ServerConfig(
        name="remote", type="http", url=server.url, timeout=10.0
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    try:
        conn.start()
        result = conn.call_tool("work", {"steps": 5})
        assert result["content"][0]["text"] == "work done"
        assert any("100%" in event and "step 5" in event for event in events)
    finally:
        _teardown(conn)
        server.stop()


def test_http_modern_tools_list_changed():
    server = _HttpServer()
    server.start()
    fired = threading.Event()
    cfg = mcp_config.ServerConfig(
        name="remote", type="http", url=server.url, timeout=10.0
    )
    conn = SdkConnection(
        cfg, secrets.resolve(cfg), AsyncRuntime(),
        on_tools_changed=lambda c: fired.set(),
    )
    try:
        conn.start()
        time.sleep(0.5)  # 等现代协议的订阅流建立
        conn.call_tool("grow", {})
        assert fired.wait(10.0), "未收到 tools/list_changed 订阅事件"
        assert "extra" in [tool["name"] for tool in conn.list_tools()]
    finally:
        _teardown(conn)
        server.stop()


class _SseServer:
    """真实 SSE（legacy）端点：SDK MCPServer.sse_app() + uvicorn。"""

    def __init__(self):
        self.server = MCPServer("fake-sse")

        @self.server.tool()
        def echo(msg: str) -> str:
            """回显输入。"""
            return f"echo: {msg}"

        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/sse"
        self._uvicorn = uvicorn.Server(uvicorn.Config(
            self.server.sse_app(), host="127.0.0.1", port=self.port, log_level="warning",
        ))
        self._thread = threading.Thread(target=self._uvicorn.run, daemon=True)

    def start(self):
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._uvicorn.started:
            time.sleep(0.05)
        assert self._uvicorn.started, "本地 SSE MCP server 启动失败"

    def stop(self):
        self._uvicorn.should_exit = True
        self._thread.join(5)


def test_sse_transport_roundtrip():
    server = _SseServer()
    server.start()
    cfg = mcp_config.ServerConfig(
        name="legacy", type="sse", url=server.url, timeout=10.0
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    try:
        conn.start()
        assert conn.alive
        assert "echo" in [tool["name"] for tool in conn.list_tools()]
        result = conn.call_tool("echo", {"msg": "hi"})
        assert result["content"][0]["text"] == "echo: hi"
    finally:
        _teardown(conn)
        server.stop()


def test_sse_transport_constructs_and_fails_on_refused():
    cfg = mcp_config.ServerConfig(
        name="legacy", type="sse", url=f"http://127.0.0.1:{_free_port()}/sse",
        timeout=2.0,
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime())
    with pytest.raises(McpError):
        conn.start()
    _teardown(conn)

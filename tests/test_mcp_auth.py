"""MCP OAuth 测试：token 存储、非交互 fail-closed 与端到端授权流程。

端到端部分起一个最小的授权服务器（发现 / DCR / authorize / token）和一个
受 Bearer 保护的 MCP server，用 ``webbrowser.open`` 的替身模拟浏览器跳转，
验证「首次交互授权 → 落盘 → 二次静默复用」的完整链路。
"""

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from mcp.server import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from smithcode.mcp import auth, secrets
from smithcode.mcp import config as mcp_config
from smithcode.mcp.connection import SdkConnection
from smithcode.mcp.errors import McpAuthError
from smithcode.mcp.runtime import AsyncRuntime
from smithcode.mcp.service import NEEDS_AUTH, McpService


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SMITHCODE_HOME", str(home))
    secrets.redactor().clear()
    yield home
    secrets.redactor().clear()


# ---------- 存储与 handler 单元测试 ----------

def _token() -> OAuthToken:
    return OAuthToken(
        access_token="acc-123456", token_type="Bearer", expires_in=3600,
        refresh_token="ref-654321",
    )


def test_token_storage_roundtrip_and_redaction(isolated):
    storage = auth.FileTokenStorage("svc")
    asyncio.run(storage.set_tokens(_token()))

    assert auth.has_tokens("svc") is True
    loaded = asyncio.run(storage.get_tokens())
    assert loaded is not None and loaded.access_token == "acc-123456"
    # token 值登记进全局脱敏器
    assert "acc-123456" not in secrets.redactor().scrub("x acc-123456 y")
    assert "ref-654321" not in secrets.redactor().scrub("x ref-654321 y")

    info = OAuthClientInformationFull(
        client_id="cid-1", redirect_uris=["http://127.0.0.1:3334/callback"]
    )
    asyncio.run(storage.set_client_info(info))
    loaded_info = asyncio.run(storage.get_client_info())
    assert loaded_info is not None and loaded_info.client_id == "cid-1"

    assert auth.clear_tokens("svc") is True
    assert auth.has_tokens("svc") is False
    assert auth.clear_tokens("svc") is False


def test_auth_file_is_private(isolated):
    import os

    storage = auth.FileTokenStorage("svc")
    asyncio.run(storage.set_tokens(_token()))
    if os.name != "nt":
        mode = auth.auth_path().stat().st_mode & 0o777
        assert mode == 0o600


def _oauth_cfg(name="svc", url="http://127.0.0.1:9/mcp"):
    return mcp_config.ServerConfig(name=name, type="http", url=url, oauth=True)


def test_non_interactive_redirect_refuses():
    session = auth.OAuthSession(_oauth_cfg(), interactive=False)
    with pytest.raises(McpAuthError):
        asyncio.run(session._redirect("http://example/authorize"))
    session.close()


def test_non_interactive_callback_refuses():
    session = auth.OAuthSession(_oauth_cfg(), interactive=False)
    with pytest.raises(McpAuthError):
        asyncio.run(session._callback())
    session.close()


# ---------- 端到端：最小授权服务器 + 受保护 MCP server ----------

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Server:
    def __init__(self, app, port):
        self._server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning",
        ))
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.port = port

    def start(self):
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not self._server.started:
            time.sleep(0.05)
        assert self._server.started

    def stop(self):
        self._server.should_exit = True
        self._thread.join(5)


class _OAuthWorld:
    """一套最小的 OAuth 授权服务器 + 受保护 MCP server。"""

    def __init__(self):
        self.as_port = _free_port()
        self.mcp_port = _free_port()
        self.as_url = f"http://127.0.0.1:{self.as_port}"
        self.mcp_url = f"http://127.0.0.1:{self.mcp_port}/mcp"
        self.access_token = "test-access-token"
        self.codes: dict = {}
        self.token_requests = 0

        self._as = _Server(self._as_app(), self.as_port)
        self._mcp = _Server(self._mcp_app(), self.mcp_port)

    # --- 授权服务器 ---
    def _as_app(self):
        async def metadata(request):
            return JSONResponse({
                "issuer": self.as_url,
                "authorization_endpoint": f"{self.as_url}/authorize",
                "token_endpoint": f"{self.as_url}/token",
                "registration_endpoint": f"{self.as_url}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            })

        async def register(request):
            body = await request.json()
            body.setdefault("token_endpoint_auth_method", "none")
            body["client_id"] = "test-client"
            return JSONResponse(body, status_code=201)

        async def authorize(request):
            params = request.query_params
            code = f"code-{len(self.codes) + 1}"
            self.codes[code] = params.get("redirect_uri")
            target = f"{params['redirect_uri']}?code={code}&state={params.get('state', '')}"
            target += f"&iss={self.as_url}"
            return RedirectResponse(target, status_code=302)

        async def token(request):
            form = await request.form()
            self.token_requests += 1
            if form.get("grant_type") == "refresh_token":
                access = f"{self.access_token}-refreshed"
            else:
                access = self.access_token
            return JSONResponse({
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "test-refresh-token",
            })

        return Starlette(routes=[
            Route("/.well-known/oauth-authorization-server", metadata),
            Route("/register", register, methods=["POST"]),
            Route("/authorize", authorize),
            Route("/token", token, methods=["POST"]),
        ])

    # --- 受保护 MCP server ---
    def _mcp_app(self):
        server = MCPServer("protected")

        @server.tool()
        def echo(msg: str) -> str:
            """回显输入。"""
            return f"echo: {msg}"

        inner = server.streamable_http_app()
        prm = {
            "resource": self.mcp_url,
            "authorization_servers": [self.as_url],
        }

        async def app(scope, receive, send):
            if scope["type"] == "http":
                path = scope["path"]
                if path.startswith("/.well-known/oauth-protected-resource"):
                    await JSONResponse(prm)(scope, receive, send)
                    return
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in scope.get("headers", [])
                }
                if headers.get("authorization") != f"Bearer {self.access_token}":
                    response = JSONResponse(
                        {"error": "unauthorized"}, status_code=401,
                        headers={"WWW-Authenticate": (
                            "Bearer resource_metadata="
                            f'"{self.mcp_url.rsplit("/mcp", 1)[0]}'
                            '/.well-known/oauth-protected-resource"'
                        )},
                    )
                    await response(scope, receive, send)
                    return
            await inner(scope, receive, send)

        return app

    def start(self):
        self._as.start()
        self._mcp.start()

    def stop(self):
        self._mcp.stop()
        self._as.stop()


@pytest.fixture
def oauth_world():
    world = _OAuthWorld()
    world.start()
    yield world
    world.stop()


def _fake_browser():
    """模拟浏览器：请求授权页并跟随 302 回到本地回调。"""
    import httpx2

    def open_url(url: str) -> bool:
        with httpx2.Client(follow_redirects=True, timeout=10.0) as client:
            client.get(url)
        return True

    return open_url


def test_oauth_end_to_end_via_service(isolated, oauth_world, monkeypatch):
    mcp_config.write_user_server(mcp_config.ServerConfig(
        name="secured", type="http", url=oauth_world.mcp_url, oauth=True, timeout=10.0,
    ))
    service = McpService()
    service.start()
    try:
        # 无 token：启动预检直接置 needs_auth，不弹浏览器
        status = service.status()[0]
        assert status.state == NEEDS_AUTH

        monkeypatch.setattr(auth.webbrowser, "open", _fake_browser())
        assert service.authorize("secured") is True
        status = service.wait_for("secured", 20)
        assert status is not None and status.state == "connected"
        assert auth.has_tokens("secured") is True
        assert service.get("secured").conn.call_tool("echo", {"msg": "hi"})[
            "content"
        ][0]["text"] == "echo: hi"
    finally:
        service.stop()


def test_oauth_tokens_reused_without_browser(isolated, oauth_world, monkeypatch):
    mcp_config.write_user_server(mcp_config.ServerConfig(
        name="secured", type="http", url=oauth_world.mcp_url, oauth=True, timeout=10.0,
    ))
    service = McpService()
    service.start()
    try:
        monkeypatch.setattr(auth.webbrowser, "open", _fake_browser())
        service.authorize("secured")
        assert service.wait_for("secured", 20).state == "connected"
    finally:
        service.stop()

    # 新连接：已有 token，非交互模式应静默复用，绝不触达浏览器
    def boom(url: str) -> bool:
        raise AssertionError("不应再次打开浏览器")

    monkeypatch.setattr(auth.webbrowser, "open", boom)
    cfg = mcp_config.ServerConfig(
        name="secured", type="http", url=oauth_world.mcp_url, oauth=True, timeout=10.0
    )
    conn = SdkConnection(cfg, secrets.resolve(cfg), AsyncRuntime(), interactive=False)
    try:
        conn.start()
        assert conn.alive
        assert "echo" in [tool["name"] for tool in conn.list_tools()]
    finally:
        conn.close()
        conn.runtime.stop()

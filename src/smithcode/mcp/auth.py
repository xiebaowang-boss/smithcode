"""MCP OAuth2.1 客户端支持：token 持久化、浏览器授权与本地回调。

官方 SDK 的 `OAuthClientProvider` 负责协议全流程（发现 → DCR → PKCE →
换 token → 刷新），本模块只补齐它需要的三块「宿主能力」：

- `FileTokenStorage`：把 token / client_info 存到 `~/.smithcode/mcp_auth.json`
  （独立于 config，0600、原子写、`anyio.to_thread` 落盘），并在读写时登记全局
  Redactor，保证 token 不进入终端与会话转录；
- `redirect_handler` / `callback_handler`：交互模式下开浏览器并在固定本地
  端口等回调；**非交互模式一律抛 `McpAuthError`**，绝不弹出浏览器（后台连接
  遇到需要授权时由 service 置 `needs_auth`，用户显式 `/mcp auth` 才走这里）；
- `has_tokens` / `clear_tokens`：启动预检与登出。

token 存的是「按服务器名」分键；redirect_uri 用固定端口（可用
`oauth.callback_port` 覆盖），避免每次授权重新 DCR 导致 client_info 里登记的
回调地址失效。
"""
from __future__ import annotations

import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anyio
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from .. import config as root_config
from .. import renderer
from .errors import McpAuthError
from .secrets import _atomic_write, redactor

# 本地回调端口：固定值保证 redirect_uri 稳定（DCR 登记的地址跨运行有效）
DEFAULT_CALLBACK_PORT = 3334
# 等待用户在浏览器完成授权的时间
AUTH_TIMEOUT = 180.0

_CLIENT_NAME = "SmithCode"


# ---------- token 持久化 ----------

def auth_path() -> Path:
    """OAuth token / client_info 的独立存储文件。"""
    return root_config.smithcode_home() / "mcp_auth.json"


def _read_all() -> dict:
    path = auth_path()
    if not path.is_file():
        return {}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 存储损坏按空处理，重新授权即可恢复
        return {}
    return data if isinstance(data, dict) else {}


def _write_all(data: dict) -> None:
    import json

    _atomic_write(auth_path(), json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _register_secret(tokens: OAuthToken | None) -> None:
    if tokens is None:
        return
    for value in (tokens.access_token, tokens.refresh_token):
        if isinstance(value, str):
            redactor().add(value)


def has_tokens(server: str) -> bool:
    """是否已存在可用 token（同步，供启动预检调用；过期与否交由 SDK 刷新）。"""
    entry = _read_all().get("servers", {}).get(server)
    return bool(isinstance(entry, dict) and entry.get("tokens"))


def clear_tokens(server: str) -> bool:
    """清除某个服务器的 token 与 client_info（下次连接重新授权）。"""
    data = _read_all()
    servers = data.get("servers")
    if not isinstance(servers, dict) or server not in servers:
        return False
    del servers[server]
    _write_all(data)
    return True


class FileTokenStorage:
    """SDK `TokenStorage` 的文件实现：四个异步方法，IO 下放到线程。"""

    def __init__(self, server: str):
        self.server = server

    def _entry(self, data: dict) -> dict:
        servers = data.get("servers")
        if not isinstance(servers, dict):
            servers = {}
            data["servers"] = servers
        entry = servers.get(self.server)
        if not isinstance(entry, dict):
            entry = {}
            servers[self.server] = entry
        return entry

    async def get_tokens(self) -> OAuthToken | None:
        data = await anyio.to_thread.run_sync(_read_all)
        raw = (data.get("servers", {}).get(self.server) or {}).get("tokens")
        if not raw:
            return None
        try:
            tokens = OAuthToken.model_validate(raw)
        except Exception:  # noqa: BLE001 旧版/损坏条目视为无 token
            return None
        _register_secret(tokens)
        return tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        _register_secret(tokens)
        payload = tokens.model_dump(mode="json", exclude_none=True)

        def write() -> None:
            data = _read_all()
            self._entry(data)["tokens"] = payload
            _write_all(data)

        await anyio.to_thread.run_sync(write)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        data = await anyio.to_thread.run_sync(_read_all)
        raw = (data.get("servers", {}).get(self.server) or {}).get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:  # noqa: BLE001 旧版/损坏条目视为未注册
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        payload = client_info.model_dump(mode="json", exclude_none=True)

        def write() -> None:
            data = _read_all()
            self._entry(data)["client_info"] = payload
            _write_all(data)

        await anyio.to_thread.run_sync(write)


# ---------- 本地回调服务器 ----------

class _CallbackHandler(BaseHTTPRequestHandler):
    queue: queue.Queue = None  # 由 _CallbackServer 注入

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        error = (params.get("error") or [None])[0]
        if error:
            detail = (params.get("error_description") or [""])[0]
            self.queue.put(McpAuthError(f"授权被拒绝: {error} {detail}".strip()))
        else:
            self.queue.put(AuthorizationCodeResult(
                code=(params.get("code") or [""])[0],
                state=(params.get("state") or [""])[0],
                iss=(params.get("iss") or [None])[0],
            ))
        body = "授权完成，可关闭此页并返回 SmithCode。".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class _CallbackServer:
    """固定端口的本地回调监听：交互授权期间常驻，连接关闭时回收。"""

    def __init__(self, port: int):
        self.queue: queue.Queue = queue.Queue()
        handler = type("_BoundCallbackHandler", (_CallbackHandler,), {"queue": self.queue})
        self.httpd = HTTPServer(("127.0.0.1", port), handler)
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, name="smithcode-oauth-callback", daemon=True
        )

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        try:
            self.httpd.shutdown()
        except Exception:  # noqa: BLE001, S110 收尾失败不阻断
            pass
        try:
            self.httpd.server_close()
        except Exception:  # noqa: BLE001, S110 收尾失败不阻断
            pass


def _pick_port(preferred: int) -> int:
    """优先用固定端口；被占用时退回临时端口（回调地址仍可用，仅 DCR 需重登记）。"""
    if preferred:
        try:
            httpd = HTTPServer(("127.0.0.1", preferred), _CallbackHandler)
        except OSError:
            renderer.current().warn(
                f"[mcp] OAuth 回调端口 {preferred} 被占用，临时改用随机端口"
            )
        else:
            httpd.server_close()
            return preferred
    probe = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    port = probe.server_address[1]
    probe.server_close()
    return port


class OAuthSession:
    """一次连接所需的 OAuth provider 与本地回调资源。"""

    def __init__(self, cfg, *, interactive: bool, timeout: float = AUTH_TIMEOUT):
        self.cfg = cfg
        self.interactive = interactive
        self.timeout = timeout
        self._server: _CallbackServer | None = None
        self.port = _pick_port(DEFAULT_CALLBACK_PORT) if interactive else DEFAULT_CALLBACK_PORT
        if interactive:
            self._server = _CallbackServer(self.port)
            self._server.start()
        # private OAuth 选项预留：当前仅 scope 可扩展，故只读取已知键
        self.provider = OAuthClientProvider(
            server_url=cfg.url,
            client_metadata=OAuthClientMetadata(
                client_name=_CLIENT_NAME,
                redirect_uris=[f"http://127.0.0.1:{self.port}/callback"],
            ),
            storage=FileTokenStorage(cfg.name),
            redirect_handler=self._redirect,
            callback_handler=self._callback,
        )

    async def _redirect(self, authorization_url: str) -> None:
        if not self.interactive:
            raise McpAuthError(
                f"MCP 服务器 {self.cfg.name} 需要 OAuth 授权："
                f"请在交互终端运行 /mcp auth {self.cfg.name}"
            )
        renderer.current().info(f"[mcp] 请在浏览器完成授权: {authorization_url}")
        opened = await anyio.to_thread.run_sync(webbrowser.open, authorization_url)
        if not opened:
            renderer.current().warn("[mcp] 无法自动打开浏览器，请手动访问上述链接")

    async def _callback(self) -> AuthorizationCodeResult:
        if self._server is None:
            raise McpAuthError("非交互模式无法完成 OAuth 授权")
        item = await anyio.to_thread.run_sync(self._wait)
        if isinstance(item, BaseException):
            raise item
        return item

    def _wait(self):
        try:
            return self._server.queue.get(timeout=self.timeout)
        except queue.Empty:
            raise McpAuthError(
                f"OAuth 授权超时（{self.timeout:g}s），请重试 /mcp auth {self.cfg.name}"
            ) from None

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

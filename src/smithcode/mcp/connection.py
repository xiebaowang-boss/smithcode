"""基于官方 SDK 的 MCP 连接：同步门面 + 共享事件循环上的 SDK `Client`。

本模块取代原先自研的 stdio JSON-RPC 客户端。设计要点：

- `SdkConnection` 对 `service` 暴露同步接口（start / list_tools / call_tool /
  close / alive / stderr_tail），内部通过 `AsyncRuntime` 把每个协程投递到
  共享 loop 线程执行；连接上下文（`Client`）长期持有，不是每次调用开关。
- 传输由 `_raw_transport` 按 `cfg.type` 构造，外层再包一层 `_monitored_transport`：
  SDK v2 不提供"连接意外断开"的推送回调（干净 EOF 被静默），因此用一条泵任务
  代读原读流，原流结束即触发 `on_closed`——等价于旧客户端读线程的崩溃检测。
- 取消沿用项目协作式令牌：`_wait` 轮询 `current_token()`，取消/超时时
  `future.cancel()` 让 CancelledError 沿 anyio 栈冒泡（不硬杀线程）。
- 结果统一 `model_dump(by_alias=True)` 成旧客户端同形的 dict，`catalog` 与
  工具注册层零改动。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import tempfile
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from pathlib import Path

import anyio
from mcp import Client, Implementation, StdioServerParameters
from mcp import MCPError as SdkMCPError
from mcp.client.auth import OAuthFlowError
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.subscriptions import ListenNotSupportedError

from .. import renderer
from ..cancel import current_token
from .auth import OAuthSession
from .errors import McpAuthError, McpError

# 握手与工具列表的超时；npx/uvx 首次运行含包下载，给足冷启动余量。
HANDSHAKE_TIMEOUT = 20.0
STARTUP_MAX = 60.0
LIST_TIMEOUT = 10.0
CLOSE_TIMEOUT = 10.0
MAX_LIST_PAGES = 100  # 分页上限：异常 server 返回重复 cursor 时不至于死循环

_POLL_INTERVAL = 0.1


class _StderrLog:
    """子进程 stderr 的落盘缓冲：child 直接写 fd，父进程按需读尾。

    SDK 的 `errlog` 会直接作为子进程 stderr 的 fd 目标，要求有真实 `fileno()`；
    故用临时文件而不是内存 buffer。
    """

    def __init__(self) -> None:
        fd, name = tempfile.mkstemp(prefix="smithcode-mcp-", suffix=".log")
        self.path = Path(name)
        self._handle = os.fdopen(fd, "w", encoding="utf-8", errors="replace")

    @property
    def stream(self):
        return self._handle

    def tail(self, lines: int = 20) -> str:
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return "\n".join(text.splitlines()[-lines:])

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


@asynccontextmanager
async def _monitored_transport(inner, on_eof):
    """包装 SDK 传输：原读流结束（EOF / 断开）时回调 `on_eof`。

    SDK v2 不把干净 EOF 暴露给任何回调，只能自己代读原读流：泵任务把消息
    转发到代理流，`async for` 正常结束或异常即认为连接终止。
    """
    async with inner as (read, write):
        send, proxy = anyio.create_memory_object_stream(0)

        async def pump() -> None:
            try:
                async with send:
                    async for item in read:
                        await send.send(item)
            except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                pass
            except Exception:  # noqa: BLE001, S110 读取侧任何异常都视为连接终止
                pass
            finally:
                on_eof()

        async with anyio.create_task_group() as tg:
            tg.start_soon(pump)
            try:
                yield proxy, write
            finally:
                tg.cancel_scope.cancel()


class SdkConnection:
    """一个 MCP 服务器的连接（start 前不可用，close 后不可复用）。"""

    def __init__(self, cfg, resolved, runtime, *, on_tools_changed=None, on_closed=None,
                 interactive: bool = False):
        self.cfg = cfg
        self.resolved = resolved
        self.runtime = runtime
        self._on_tools_changed = on_tools_changed
        self._on_closed = on_closed
        self._interactive = interactive  # 是否允许弹出浏览器完成 OAuth（仅 /mcp auth）
        self._auth_session: OAuthSession | None = None

        self.transport = cfg.type
        self.server_info: dict = {}
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._watch_task: asyncio.Task | None = None  # 现代协议的订阅监听任务
        self._stderr = _StderrLog() if cfg.type in ("stdio", "local") else None
        self._notes: list = []
        self._malformed = 0
        self._connected = False
        self._eof = False
        self._closed = False
        self._closing = False
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self) -> dict:
        """启动传输并完成 SDK 握手；失败抛 McpError。"""
        self.runtime.start()
        startup = max(HANDSHAKE_TIMEOUT, min(self.cfg.timeout, STARTUP_MAX))
        try:
            info = self.runtime.run(self._aopen(), timeout=startup)
        except concurrent.futures.TimeoutError as e:
            self.close()
            raise McpError(
                f"握手失败: 超时（{startup:g}s）"
                "（首次运行可能在下载依赖，可在终端预热或稍后 /mcp reconnect 重试）"
            ) from e
        except McpError:
            self.close()
            raise
        except Exception as e:  # 统一翻译为面向用户的启动错误
            self.close()
            auth_error = _auth_error(e)
            if auth_error is not None:
                raise auth_error from e
            raise McpError(f"握手失败: {_describe(e)}") from e

        self.server_info = info
        return info

    def close(self) -> None:
        """关闭连接；幂等。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._closing = True
        stack = self._stack
        if stack is not None:
            try:
                self.runtime.run(self._aclose(), timeout=CLOSE_TIMEOUT)
            except Exception:  # noqa: BLE001, S110 收尾失败不阻断
                pass
        if self._stderr is not None:
            self._stderr.close()
        if self._auth_session is not None:
            self._auth_session.close()
            self._auth_session = None

    @property
    def alive(self) -> bool:
        return self._connected and not self._closed and not self._eof

    # ---------- 协议操作 ----------

    def list_tools(self) -> list:
        timeout = max(self.cfg.timeout, LIST_TIMEOUT)
        return self._wait(self.runtime.submit(self._alist()), timeout, "列出工具")

    def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
        limit = self.cfg.timeout if timeout is None else timeout
        coro = self._acall(name, arguments or {})
        return self._wait(self.runtime.submit(coro), limit, f"调用 {name}")

    # ---------- 诊断 ----------

    def stderr_tail(self, lines: int = 20) -> str:
        parts = list(self._notes[-lines:])
        if self._stderr is not None:
            tail = self._stderr.tail(lines)
            if tail:
                parts.append(tail)
        return "\n".join(parts)

    @property
    def malformed_lines(self) -> int:
        return self._malformed

    # ---------- 异步实现 ----------

    async def _aopen(self) -> dict:
        stack = AsyncExitStack()
        try:
            transport = await self._open_transport(stack)
            client = self._build_client(transport)
            client = await stack.enter_async_context(client)
        except BaseException:
            await stack.aclose()
            raise
        self._client = client
        self._stack = stack
        self._connected = True
        # 现代协议（2026-07-28+）的工具列表变更走订阅流；旧协议由 message_handler 承担
        self._watch_task = asyncio.create_task(self._watch_tools())
        info = client.server_info
        if info is None:
            return {}
        return info.model_dump(by_alias=True, exclude_none=True)

    async def _aclose(self) -> None:
        watch, self._watch_task = self._watch_task, None
        if watch is not None and not watch.done():
            watch.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await watch
        stack, self._stack = self._stack, None
        if stack is not None:
            await stack.aclose()

    async def _alist(self) -> list:
        tools: list = []
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            try:
                result = await self._client.list_tools(cursor=cursor)
            except Exception as e:
                raise _translate(e) from e
            for tool in result.tools:
                tools.append(tool.model_dump(by_alias=True, exclude_none=True))
            cursor = result.next_cursor
            if not cursor:
                break
        return tools

    async def _acall(self, name: str, arguments: dict) -> dict:
        try:
            result = await self._client.call_tool(
                name, arguments, progress_callback=self._progress_reporter(name)
            )
        except Exception as e:
            raise _translate(e) from e
        return result.model_dump(by_alias=True, exclude_none=True)

    def _progress_reporter(self, tool: str):
        """进度回调：按 10% 里程碑上报，避免刷屏；total 未知时不展示。"""
        state = {"pct": None}

        async def report(progress: float, total: float | None, message: str | None) -> None:
            if not total:
                return
            try:
                pct = int(progress * 100 / total)
            except (TypeError, ZeroDivisionError):
                return
            if state["pct"] is not None and pct - state["pct"] < 10:
                return
            state["pct"] = pct
            text = f"[mcp] {self.cfg.name}.{tool} 进度 {pct}%"
            if message:
                text += f"（{message}）"
            renderer.current().info(text)

        return report

    async def _watch_tools(self) -> None:
        """现代协议的 tools/list_changed 订阅；旧协议静默退出（由 message_handler 承担）。"""
        try:
            async with self._client.listen(tools_list_changed=True) as subscription:
                async for _event in subscription:
                    if self._on_tools_changed is not None:
                        try:
                            self._on_tools_changed(self)
                        except Exception:  # noqa: BLE001, S110 通知失败不影响订阅
                            pass
        except ListenNotSupportedError:
            return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 订阅不可用不影响连接
            return

    async def _on_message(self, message) -> None:
        """SDK message_handler：接受服务端通知或传输层异常。"""
        if isinstance(message, Exception):
            self._malformed += 1
            self._notes.append(f"[传输错误] {type(message).__name__}: {message}")
            return
        if (getattr(message, "method", None) == "notifications/tools/list_changed"
                and self._on_tools_changed is not None):
            try:
                self._on_tools_changed(self)
            except Exception:  # noqa: BLE001, S110 通知处理失败不影响读循环
                pass

    # ---------- 传输构造 ----------

    def _build_client(self, transport) -> Client:
        from .. import __version__

        return Client(
            transport,
            client_info=Implementation(name="smithcode", version=__version__),
            message_handler=self._on_message,
            mode="auto",
        )

    def _monitored(self, inner):
        return _monitored_transport(inner, self._handle_eof)

    async def _open_transport(self, stack):
        """构造传输；需要随连接生命周期的资源（如 HTTP 客户端）挂到 stack。"""
        kind = self.cfg.type
        if kind == "stdio":
            return self._monitored(self._stdio_transport())
        if kind == "http":
            import httpx2

            auth = None
            if self.cfg.oauth:
                self._auth_session = OAuthSession(self.cfg, interactive=self._interactive)
                auth = self._auth_session.provider
            http_client = httpx2.AsyncClient(
                headers=self.resolved.headers or None,
                auth=auth,
                timeout=httpx2.Timeout(30.0, read=max(300.0, self.cfg.timeout)),
            )
            await stack.enter_async_context(http_client)  # 我们自己拥有并关闭它
            return self._monitored(
                streamable_http_client(self.cfg.url, http_client=http_client)
            )
        if kind == "sse":
            return self._monitored(sse_client(
                self.cfg.url,
                headers=self.resolved.headers or None,
                timeout=30.0,
                sse_read_timeout=max(300.0, self.cfg.timeout),
            ))
        raise McpError(f"暂不支持的传输类型: {kind!r}")

    def _stdio_transport(self):
        command = list(self.resolved.command)
        if not command:
            raise McpError("命令为空")
        # 保持旧行为：父进程环境 + 展开后的服务器 env（SDK 默认只继承白名单）
        env = {**os.environ, **self.resolved.env}
        params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            env=env,
            cwd=self.resolved.cwd or None,
            encoding_error_handler="replace",
        )
        return stdio_client(params, errlog=self._stderr.stream)

    # ---------- 内部 ----------

    def _handle_eof(self) -> None:
        """传输读到 EOF（进程退出 / 远端断开）：非主动关闭时通知服务层。"""
        if self._closing or self._eof:
            return
        self._eof = True
        if self._on_closed is not None:
            try:
                self._on_closed(self)
            except Exception:  # noqa: BLE001, S110 回调失败不影响收尾
                pass

    def _wait(self, future, timeout: float, what: str):
        """同步等待一个已投递的协程：轮询取消令牌与超时。"""
        deadline = time.monotonic() + timeout
        token = current_token()
        while True:
            try:
                return future.result(timeout=_POLL_INTERVAL)
            except concurrent.futures.TimeoutError:
                if token is not None and token.cancelled:
                    future.cancel()
                    raise McpError("用户中断")
                if time.monotonic() >= deadline:
                    future.cancel()
                    raise McpError(f"{what}超时（{timeout:g}s）")
                if self._eof:
                    raise McpError("连接已断开")


def _translate(exc: Exception) -> McpError:
    """把 SDK / 底层异常翻译成子系统统一异常。"""
    if isinstance(exc, McpError):
        return exc
    auth_error = _auth_error(exc)
    if auth_error is not None:
        return auth_error
    if isinstance(exc, SdkMCPError):
        return McpError(str(exc))
    return McpError(f"{type(exc).__name__}: {exc}")


def _auth_error(exc: BaseException) -> McpAuthError | None:
    """从（可能被 ExceptionGroup 包裹的）异常树里提取授权失败。"""
    found = _find_exc(exc, (McpAuthError, OAuthFlowError))
    if found is None:
        return None
    if isinstance(found, McpAuthError):
        return found
    return McpAuthError(f"授权失败: {found}")


def _find_exc(exc: BaseException, kinds: tuple):
    """深度优先遍历异常树（ExceptionGroup / cause / context），找到第一个匹配。"""
    seen: set = set()
    stack = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kinds):
            return current
        stack.extend(getattr(current, "exceptions", ()) or ())
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return None


def _describe(exc: Exception) -> str:
    if isinstance(exc, FileNotFoundError):
        return f"找不到命令（检查 PATH 或改用绝对路径）: {exc}"
    if isinstance(exc, SdkMCPError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"

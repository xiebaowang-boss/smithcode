"""stdio 传输的 MCP 客户端：同步线程模型下的双向 JSON-RPC 连接。

实现要点：

- 每个连接一个守护读线程，负责把 stdout 的 JSON-RPC 消息路由到
  pending 表（响应）、通知回调（tools/list_changed 等）或统一的
  "方法不存在"应答（server 反向请求，MVP 不支持 elicitation）；
- 请求/响应用 Event 等待 + 轮询超时，轮询点同时检查当前线程的取消
  令牌（Esc 中断）：取消时发 `notifications/cancelled` 并立刻返回；
- stderr 由独立线程持续排空进环形缓冲（不排空会阻塞 server），
  `/mcp logs` 与失败诊断从这里取；
- 关闭走"关 stdin → 宽限 → 终止进程树"，复用 process.terminate_tree，
  Windows npx 等 .cmd 入口用 shutil.which 解析。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from ..cancel import current_token
from ..process import terminate_tree
from .errors import McpError

# 客户端声明的协议版本（2025-06-18 修订；新版无状态协议探测留待后续）
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "smithcode"

# 握手与工具列表的超时；npx/uvx 首次运行包含包下载（实测 5-30s），给足冷启动余量。
# 握手预算 = clamp(服务器配置 timeout, HANDSHAKE_TIMEOUT, STARTUP_MAX)：
# 连接在后台线程执行，宽松预算只影响失败状态的出现时间，不阻塞界面。
HANDSHAKE_TIMEOUT = 20.0
STARTUP_MAX = 60.0
LIST_TIMEOUT = 10.0

_POLL_INTERVAL = 0.05
_CLOSE_GRACE = 2.0
_STDERR_MAX_LINES = 200


class _Pending:
    """一次待响应请求：Event + 结果槽位。"""

    __slots__ = ("error", "event", "result")

    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error: McpError | None = None


class StdioConnection:
    """一个 stdio MCP 服务器的连接（start 前不可用，close 后不可复用）。"""

    def __init__(self, name: str, command: list, env: dict | None = None,
                 cwd: str = "", timeout: float = 60.0,
                 on_tools_changed=None, on_closed=None):
        self.name = name
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = float(timeout)
        self._on_tools_changed = on_tools_changed
        self._on_closed = on_closed

        self._proc: subprocess.Popen | None = None
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._counter = 0
        self._closed = threading.Event()
        self._stderr = deque(maxlen=_STDERR_MAX_LINES)
        self._malformed = 0
        self.server_info: dict = {}

    # ---------- 生命周期 ----------

    def start(self) -> dict:
        """启动子进程并完成 initialize 握手；失败抛 McpError。"""
        argv = _resolve_command(self.command)
        env = dict(os.environ)
        env.update(self.env)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                cwd=self.cwd or None,
                env=env,
                start_new_session=(os.name != "nt"),
            )
        except OSError as e:
            raise McpError(f"启动失败: {e}") from e

        threading.Thread(
            target=self._stdout_loop, name=f"mcp-{self.name}-stdout", daemon=True
        ).start()
        threading.Thread(
            target=self._stderr_loop, name=f"mcp-{self.name}-stderr", daemon=True
        ).start()

        try:
            startup_timeout = max(HANDSHAKE_TIMEOUT, min(self.timeout, STARTUP_MAX))
            result = self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": _version()},
            }, timeout=startup_timeout)
        except McpError as e:
            self.close()
            detail = str(e)
            if "超时" in detail:
                detail += "（首次运行可能在下载依赖，可在终端预热或稍后 /mcp reconnect 重试）"
            raise McpError(f"握手失败: {detail}") from e

        self.server_info = result if isinstance(result, dict) else {}
        self.notify("notifications/initialized", {})
        return self.server_info

    def close(self) -> None:
        """关闭连接：关 stdin → 宽限退出 → 终止进程树；幂等。"""
        with self._close_lock:
            if self._closed.is_set():
                return
            self._closed.set()
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=_CLOSE_GRACE)
        except subprocess.TimeoutExpired:
            terminate_tree(proc)
        self._fail_pending(McpError("连接已关闭"))

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and not self._closed.is_set()
            and self._proc.poll() is None
        )

    # ---------- 请求 / 通知 ----------

    def request(self, method: str, params: dict, timeout: float | None = None) -> dict:
        """发送请求并等待响应；超时/取消/连接断开抛 McpError。"""
        if not self.alive:
            raise McpError("连接不可用")
        with self._lock:
            self._counter += 1
            request_id = self._counter
            pending = _Pending()
            self._pending[request_id] = pending

        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        token = current_token()
        while True:
            if pending.event.wait(_POLL_INTERVAL):
                break
            if token is not None and token.cancelled:
                self._cancel_request(request_id)
                raise McpError("用户中断")
            if time.monotonic() >= deadline:
                self._cancel_request(request_id)
                raise McpError(f"超时（{timeout if timeout is not None else self.timeout:g}s）")
            if not self.alive:
                self._cancel_request(request_id)
                raise McpError("连接已断开")

        if pending.error is not None:
            raise pending.error
        result = pending.result
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params:
            message["params"] = params
        self._write(message)

    # ---------- 协议操作 ----------

    def list_tools(self) -> list:
        """拉取工具列表（自动翻页）。"""
        tools: list = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params, timeout=LIST_TIMEOUT)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                return tools

    def call_tool(self, name: str, arguments: dict, timeout: float | None = None) -> dict:
        """调用一个工具，返回原始 CallToolResult（由 catalog 格式化）。"""
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )

    # ---------- 诊断 ----------

    def stderr_tail(self, lines: int = 20) -> str:
        with self._lock:
            items = list(self._stderr)[-lines:]
        return "\n".join(items)

    @property
    def malformed_lines(self) -> int:
        """stdout 中无法解析为 JSON-RPC 的行数（server 未按规范使用 stderr 的信号）。"""
        return self._malformed

    # ---------- 内部：IO 与路由 ----------

    def _write(self, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None:
                raise McpError("连接不可用")
            try:
                proc.stdin.write(payload.encode("utf-8"))
                proc.stdin.flush()
            except OSError as e:
                raise McpError(f"写入失败: {e}") from e

    def _stdout_loop(self) -> None:
        proc = self._proc
        try:
            while True:
                raw = proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self._malformed += 1
                    self._note_stderr(f"[非 JSON 输出] {line[:200]}")
                    continue
                if isinstance(message, dict):
                    self._route(message)
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            self._fail_pending(McpError("服务器进程已退出"))
            if self._on_closed is not None:
                try:
                    self._on_closed(self.name)
                except Exception:  # noqa: BLE001, S110 回调失败不影响收尾
                    pass

    def _stderr_loop(self) -> None:
        proc = self._proc
        try:
            while True:
                raw = proc.stderr.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line:
                    self._note_stderr(line)
        except (OSError, ValueError):
            pass

    def _note_stderr(self, line: str) -> None:
        with self._lock:
            self._stderr.append(line)

    def _route(self, message: dict) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and ("result" in message or "error" in message):
            with self._lock:
                pending = self._pending.pop(request_id, None)
            if pending is None:
                return  # 超时/取消后的迟到响应，忽略
            error = message.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                text = error.get("message") or "未知错误"
                pending.error = McpError(f"服务器错误 {code}: {text}")
            else:
                pending.result = message.get("result")
            pending.event.set()
            return

        if method is None:
            return
        if request_id is not None:
            # server 反向请求（如 elicitation）：MVP 统一答"方法不存在"
            self._write({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found"},
            })
            return
        if method == "notifications/tools/list_changed" and self._on_tools_changed is not None:
            try:
                self._on_tools_changed(self.name)
            except Exception:  # noqa: BLE001, S110 通知处理失败不影响读线程
                pass
        # 其余通知（progress / message 等）MVP 静默忽略

    def _cancel_request(self, request_id: int) -> None:
        with self._lock:
            self._pending.pop(request_id, None)
        try:
            self.notify("notifications/cancelled", {"requestId": request_id, "reason": "client"})
        except McpError:
            pass

    def _fail_pending(self, error: McpError) -> None:
        with self._lock:
            pendings = list(self._pending.values())
            self._pending.clear()
        for pending in pendings:
            pending.error = error
            pending.event.set()


def _resolve_command(command: list) -> list:
    """解析可执行文件：PATH 查找（Windows 含 .cmd）或已存在的路径。"""
    if not command:
        raise McpError("命令为空")
    executable = str(command[0])
    resolved = shutil.which(executable)
    if resolved:
        return [resolved] + [str(item) for item in command[1:]]
    if Path(executable).is_file():
        return [str(item) for item in command]
    raise McpError(f"找不到命令 {executable!r}（检查 PATH 或改用绝对路径）")


def _version() -> str:
    from .. import __version__

    return __version__

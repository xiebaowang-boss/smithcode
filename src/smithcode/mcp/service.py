"""MCP 会话级服务：连接生命周期、工具注册与调用路由。

对外是命令层与 Agent 的唯一入口：

- `start()` / `stop()` 由 Agent 生命周期调用；启动时后台并发连接所有
  启用且密钥齐全的服务器，单个失败只影响自己并给出可诊断错误；
- 连接成功后把工具注册进 `tools/base` 的动态注册表（全局唯一暴露名、
  默认 serial、`describe` 生成终端摘要），断开时反注册，模型侧无感知；
- `call()` 是动态注册闭包的执行目标：查连接 → 调用 → 结果映射脱敏；
- 状态与工具清单供 `/mcp` 面板与 TUI 向导读取。

线程模型：连接/刷新在专用线程池执行；注册表增删走 base 的锁；状态修改
都在本服务锁内，读方拿的是快照。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .. import renderer
from ..tools import DYNAMIC, register_dynamic, unregister_dynamic
from . import auth, catalog
from . import config as mcp_config
from .errors import McpAuthError, McpConfigError, McpError
from .factory import create_connection
from .runtime import AsyncRuntime
from .secrets import redactor, resolve

# 状态取值
PENDING = "pending"
CONNECTING = "connecting"
CONNECTED = "connected"
FAILED = "failed"
MISSING_ENV = "missing_env"
NEEDS_AUTH = "needs_auth"
DISABLED = "disabled"
DISCONNECTED = "disconnected"

_SCOPE_LABELS = {"user": "全局", "project": "项目"}

_STATE_LABELS = {
    PENDING: "等待连接",
    CONNECTING: "连接中",
    CONNECTED: "已连接",
    FAILED: "连接失败",
    MISSING_ENV: "缺少密钥",
    NEEDS_AUTH: "需要授权",
    DISABLED: "已停用",
    DISCONNECTED: "已断开",
}


@dataclass
class ServerStatus:
    """`/mcp` 展示用的服务器状态快照。"""

    name: str
    scope: str
    state: str
    command: str = ""
    transport: str = "stdio"
    url: str = ""
    oauth: bool = False
    tool_count: int = 0
    error: str = ""
    missing: list = field(default_factory=list)

    @property
    def state_label(self) -> str:
        return _STATE_LABELS.get(self.state, self.state)

    @property
    def scope_label(self) -> str:
        return _SCOPE_LABELS.get(self.scope, self.scope)


class _Entry:
    """一个服务器的运行时状态（配置 + 连接 + 已注册工具）。"""

    def __init__(self, cfg: mcp_config.ServerConfig):
        self.cfg = cfg
        self.conn = None
        self.state = PENDING
        self.error = ""
        self.missing: list = []
        self.tools: list = []  # list[catalog.ToolSpec]
        # 连接代际：每次发起连接 / 断开递增；异步连接回写前校验代际，避免
        # "连接仍在握手时被 disable/remove" 的结果覆盖（旧实现的竞态根因）。
        self.generation = 0

    @property
    def exposed_names(self) -> list:
        return [spec.exposed for spec in self.tools]


class McpService:
    """MCP 服务器的会话级管理器（进程内单例由 Agent 持有）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict = {}
        self._order: list = []
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="smithcode-mcp")
        self._runtime = AsyncRuntime()  # 所有连接共用的 asyncio loop 线程
        self._started = False
        self._stopped = False
        self.diagnostics: list = []
        self.project_path = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """装载配置并后台连接（Agent.start() 调用；重复调用无副作用）。"""
        with self._lock:
            if self._started or self._stopped:
                return
            self._started = True
        self._runtime.start()
        loaded = mcp_config.load_servers()
        self.diagnostics = list(loaded.diagnostics)
        self.project_path = loaded.project_path
        for message in self.diagnostics:
            renderer.current().warn(f"[mcp] {message}")
        project_names = [cfg.name for cfg in loaded.servers if cfg.scope == "project"]
        if project_names:
            # 项目配置随仓库分发且会拉起本机进程：启动时给出一次可见提示（不做门控）
            renderer.current().warn(
                f"[mcp] 已加载项目配置（{', '.join(project_names)}）："
                "MCP 服务器将以你的本机权限运行，请确认仓库来源可信"
            )
        with self._lock:
            for cfg in loaded.servers:
                self._entries[cfg.name] = _Entry(cfg)
                self._order.append(cfg.name)
        for cfg in loaded.servers:
            if not cfg.enabled:
                self._set_state(cfg.name, DISABLED)
                continue
            missing = self._check_missing(cfg)
            if missing:
                entry = self._entries[cfg.name]
                entry.missing = missing
                entry.error = f"缺少环境变量: {', '.join(missing)}"
                self._set_state(cfg.name, MISSING_ENV, entry.error)
                renderer.current().warn(f"[mcp] {cfg.name}: {entry.error}")
                continue
            if cfg.oauth and not auth.has_tokens(cfg.name):
                entry = self._entries[cfg.name]
                entry.error = f"需要 OAuth 授权：运行 /mcp auth {cfg.name}"
                self._set_state(cfg.name, NEEDS_AUTH, entry.error)
                renderer.current().warn(f"[mcp] {cfg.name}: {entry.error}")
                continue
            self._pool.submit(self._connect, cfg.name)

    def stop(self) -> None:
        """关闭全部连接并停止线程池（Agent.close() / 进程退出调用）。"""
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            entries = list(self._entries.values())
        for entry in entries:
            self._disconnect(entry, DISCONNECTED)
        self._runtime.stop()
        self._pool.shutdown(wait=False, cancel_futures=True)

    def wait(self, timeout: float = 5.0) -> bool:
        """等待所有连接进入终态（测试与一次性任务首轮可选）；返回是否已就绪。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = {status.state for status in self.status()}
            if not (states & {PENDING, CONNECTING}):
                return True
            time.sleep(0.05)
        return False

    def wait_for(self, name: str, timeout: float = 15.0) -> ServerStatus | None:
        """等待单个服务器进入终态。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._status_of(name)
            if status is None or status.state not in (PENDING, CONNECTING):
                return status
            time.sleep(0.05)
        return self._status_of(name)

    # ---------- 查询 ----------

    def status(self) -> list:
        """全部服务器状态快照（配置顺序）。"""
        with self._lock:
            names = list(self._order)
            entries = dict(self._entries)
        result = []
        for name in names:
            entry = entries.get(name)
            if entry is not None:
                result.append(_snapshot(entry))
        return result

    def get(self, name: str):
        with self._lock:
            return self._entries.get(name)

    def tools(self, name: str) -> list:
        """某个服务器已暴露的工具（展示用）。"""
        entry = self.get(name)
        if entry is None:
            return []
        return [
            {
                "original": spec.original,
                "exposed": spec.exposed,
                "description": spec.description,
                "read_only": spec.read_only,
            }
            for spec in entry.tools
        ]

    def logs(self, name: str, lines: int = 20) -> str:
        """最近的 server stderr（已脱敏）。"""
        entry = self.get(name)
        if entry is None or entry.conn is None:
            return ""
        return redactor().scrub(entry.conn.stderr_tail(lines))

    def missing_secrets(self, name: str) -> list:
        """某个服务器缺失的环境变量名。"""
        entry = self.get(name)
        if entry is None:
            return []
        return list(entry.missing)

    # ---------- 配置变更 ----------

    def add(self, cfg: mcp_config.ServerConfig, scope: str | None = None) -> ServerStatus:
        """写入配置并后台连接；同名跨作用域冲突时明确报错。"""
        scope = scope or cfg.scope
        if scope not in _SCOPE_LABELS:
            raise McpConfigError(f"未知作用域: {scope!r}（可选 user / project）")
        cfg.scope = scope

        with self._lock:
            existing = self._entries.get(cfg.name)
            if existing is not None and existing.cfg.scope != scope:
                raise McpConfigError(
                    f"服务器 {cfg.name!r} 已存在于{_SCOPE_LABELS[existing.cfg.scope]}配置"
                    f"（{existing.cfg.source}），请先删除后再以{_SCOPE_LABELS[scope]}添加"
                )

        if scope == "project":
            mcp_config.write_project_server(cfg)
        else:
            mcp_config.write_user_server(cfg)

        with self._lock:
            old = self._entries.get(cfg.name)
            if old is not None:
                self._disconnect(old, DISCONNECTED)
            self._entries[cfg.name] = _Entry(cfg)
            if cfg.name not in self._order:
                self._order.append(cfg.name)
        self._pool.submit(self._connect, cfg.name)
        return self._status_of(cfg.name)

    def remove(self, name: str) -> bool:
        """断开并从配置中删除（按条目来源作用域写入删除）。"""
        with self._lock:
            entry = self._entries.pop(name, None)
            if name in self._order:
                self._order.remove(name)
        if entry is None:
            return False
        self._disconnect(entry, DISCONNECTED)
        if entry.cfg.scope == "project":
            mcp_config.remove_project_server(name)
        else:
            mcp_config.remove_user_server(name)
        return True

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """启停：`enabled` 写进定义该服务器的配置条目（用户 TOML / 项目 JSON）。"""
        entry = self.get(name)
        if entry is None:
            return False
        mcp_config.set_enabled(name, bool(enabled))
        if enabled:
            self._set_state(name, PENDING)
            entry.error = ""
            self._pool.submit(self._connect, name)
        else:
            self._disconnect(entry, DISABLED)
        return True

    def reconnect(self, name: str) -> bool:
        entry = self.get(name)
        if entry is None:
            return False
        self._disconnect(entry, DISCONNECTED)
        self._set_state(name, PENDING)
        self._pool.submit(self._connect, name)
        return True

    def authorize(self, name: str) -> bool:
        """交互式 OAuth 授权：后台连接允许弹浏览器（`/mcp auth` 调用）。"""
        entry = self.get(name)
        if entry is None:
            return False
        if not entry.cfg.oauth or entry.cfg.type not in ("http", "sse"):
            return False
        self._disconnect(entry, DISCONNECTED)
        self._set_state(name, PENDING)
        self._pool.submit(self._connect, name, True)
        return True

    def reload(self) -> list:
        """重新装载配置并 diff：新增连接、删除断开、指纹变化重连。"""
        loaded = mcp_config.load_servers()
        self.diagnostics = list(loaded.diagnostics)
        self.project_path = loaded.project_path
        wanted = {cfg.name: cfg for cfg in loaded.servers}
        with self._lock:
            current = dict(self._entries)
        for name in list(current):
            if name not in wanted:
                self.remove(name)
        for name, cfg in wanted.items():
            existing = current.get(name)
            if existing is None:
                with self._lock:
                    self._entries[name] = _Entry(cfg)
                    if name not in self._order:
                        self._order.append(name)
                self._pool.submit(self._connect, name)
            elif existing.cfg.fingerprint != cfg.fingerprint:
                with self._lock:
                    existing.cfg = cfg
                self.reconnect(name)
            elif cfg.enabled and existing.state in (DISABLED,):
                self.set_enabled(name, True)
        return self.status()

    # ---------- 调用路由 ----------

    def call(self, server: str, tool: str, args: dict) -> str:
        """动态注册闭包的执行目标；任何失败都翻译成给模型的错误文本。"""
        entry = self.get(server)
        conn = entry.conn if entry is not None else None
        if conn is None or not conn.alive:
            return f"错误: MCP 服务器 {server} 未连接"
        try:
            result = conn.call_tool(tool, args, timeout=entry.cfg.timeout)
        except McpError as e:
            return f"错误: MCP {server}.{tool}: {e}"
        return catalog.format_result(result, redactor())

    # ---------- 内部 ----------

    def _connect(self, name: str, interactive: bool = False) -> None:
        """连接一个服务器（线程池中执行）：握手 → 拉工具 → 注册。

        异步竞态防护：进入时记录 entry 代际，任何回写（连接、工具、状态）
        前都用 `_is_current` / 代际比对确认这条连接仍是当前有效意图——避免
        "握手期间被 disable/remove，结果仍把它连上并注册" 的旧缺陷。
        `interactive=True` 仅由 `/mcp auth` 触发，允许 OAuth 弹浏览器。
        """
        with self._lock:
            if self._stopped:
                return
            entry = self._entries.get(name)
            if entry is None:
                return
            entry.generation += 1
            generation = entry.generation
        self._set_state(name, CONNECTING)
        resolved = resolve(entry.cfg)
        if resolved.missing:
            if not self._is_current(name, entry, generation):
                return
            entry.missing = resolved.missing
            entry.error = f"缺少环境变量: {', '.join(resolved.missing)}"
            self._set_state(name, MISSING_ENV, entry.error)
            renderer.current().warn(f"[mcp] {name}: {entry.error}")
            return
        entry.missing = []

        if entry.cfg.oauth and not interactive and not auth.has_tokens(name):
            if not self._is_current(name, entry, generation):
                return
            entry.error = f"需要 OAuth 授权：运行 /mcp auth {name}"
            self._set_state(name, NEEDS_AUTH, entry.error)
            renderer.current().warn(f"[mcp] {name}: {entry.error}")
            return

        conn = create_connection(
            entry.cfg, resolved, self._runtime,
            on_tools_changed=self._on_tools_changed,
            on_closed=self._on_closed,
            interactive=interactive,
        )
        try:
            conn.start()
            tool_defs = conn.list_tools()
        except McpAuthError as e:
            conn.close()
            if not self._is_current(name, entry, generation):
                return
            entry.error = str(e)
            self._set_state(name, NEEDS_AUTH, entry.error)
            renderer.current().warn(f"[mcp] {name}: {entry.error}")
            return
        except McpError as e:
            tail = conn.stderr_tail(5)
            conn.close()
            if not self._is_current(name, entry, generation):
                return
            entry.error = str(e)
            if tail:
                entry.error += f"（stderr: {redactor().scrub(tail)}）"
            self._set_state(name, FAILED, entry.error)
            renderer.current().error(f"[mcp] {name} 连接失败: {entry.error}")
            return
        except Exception as e:  # noqa: BLE001 未预期异常也要落到 FAILED，不能停在 CONNECTING
            conn.close()
            if not self._is_current(name, entry, generation):
                return
            entry.error = f"{type(e).__name__}: {e}"
            self._set_state(name, FAILED, entry.error)
            renderer.current().error(f"[mcp] {name} 连接失败: {entry.error}")
            return

        with self._lock:
            current = (
                not self._stopped
                and self._entries.get(name) is entry
                and entry.generation == generation
                and entry.state != DISABLED
            )
            if current:
                entry.conn = conn
        if not current:
            conn.close()  # 期间被 disable / remove / 重连：丢弃这条连接
            return
        with self._lock:
            if self._entries.get(name) is not entry or entry.conn is not conn:
                conn.close()
                return
            self._sync_tools(entry, tool_defs)
            self._set_state(name, CONNECTED)
        renderer.current().success(
            f"[mcp] {name} 已连接（{len(entry.tools)} 个工具）"
        )

    def _refresh_tools(self, conn) -> None:
        if not conn.alive:
            return
        name = conn.cfg.name
        try:
            tool_defs = conn.list_tools()
        except McpError:
            return
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or entry.conn is not conn:
                return
            self._sync_tools(entry, tool_defs)
            count = len(entry.tools)
        renderer.current().info(f"[mcp] {name} 工具列表已更新（{count} 个工具）")

    def _on_tools_changed(self, conn) -> None:
        self._pool.submit(self._refresh_tools, conn)

    def _on_closed(self, conn) -> None:
        """连接非主动断开（进程退出 / 远端断开）：按连接身份判定，避免误伤新连接。"""
        name = conn.cfg.name
        with self._lock:
            entry = self._entries.get(name)
            if entry is None or entry.conn is not conn:
                return
            entry.conn = None
            entry.generation += 1
        self._unregister(entry)
        entry.tools = []
        self._set_state(name, FAILED, "服务器连接已断开")
        renderer.current().error(f"[mcp] {name}: 服务器连接已断开")

    def _disconnect(self, entry: _Entry, state: str) -> None:
        with self._lock:
            entry.generation += 1  # 使在途连接的回写失效
            conn, entry.conn = entry.conn, None
        if conn is not None:
            conn.close()
        self._unregister(entry)
        entry.tools = []
        entry.missing = []
        self._set_state(entry.cfg.name, state, entry.error if state == FAILED else "")

    def _is_current(self, name: str, entry: _Entry, generation: int) -> bool:
        with self._lock:
            return (
                not self._stopped
                and self._entries.get(name) is entry
                and entry.generation == generation
            )

    def _sync_tools(self, entry: _Entry, tool_defs: list) -> None:
        """原子替换一个服务器的工具集：先反注册旧名，再注册新名。"""
        self._unregister(entry)
        taken = set(DYNAMIC)
        specs = []
        for tool_def in tool_defs:
            if not isinstance(tool_def, dict):
                continue
            spec = catalog.build_spec(entry.cfg.name, tool_def, taken)
            specs.append(spec)
            schema = spec.to_schema()
            register_dynamic(
                schema,
                self._make_func(entry.cfg.name, spec.original),
                serial=True,
                describe=self._make_describer(entry.cfg.name, spec.original),
                display="inline",
            )
        entry.tools = specs

    def _unregister(self, entry: _Entry) -> None:
        for exposed in entry.exposed_names:
            unregister_dynamic(exposed)

    def _make_func(self, server: str, tool: str):
        def run(**kwargs) -> str:
            return self.call(server, tool, kwargs)
        return run

    @staticmethod
    def _make_describer(server: str, tool: str):
        def describe(args: dict) -> str:
            return f"mcp {server}.{tool}"
        return describe

    def _check_missing(self, cfg: mcp_config.ServerConfig) -> list:
        return resolve(cfg).missing

    def _set_state(self, name: str, state: str, error: str = "") -> None:
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                return
            entry.state = state
            if error:
                entry.error = error

    def _status_of(self, name: str) -> ServerStatus | None:
        entry = self.get(name)
        return _snapshot(entry) if entry is not None else None


def _snapshot(entry: _Entry) -> ServerStatus:
    return ServerStatus(
        name=entry.cfg.name,
        scope=entry.cfg.scope,
        state=entry.state,
        command=entry.cfg.target,
        transport=entry.cfg.type,
        url=entry.cfg.url,
        oauth=entry.cfg.oauth,
        tool_count=len(entry.tools),
        error=entry.error,
        missing=list(entry.missing),
    )

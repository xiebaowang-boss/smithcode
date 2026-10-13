"""`smith serve`：本机守护进程（Tauri 2 sidecar 的目标）。

进程模型（对齐 OpenCode `spawnLocalServer` + `checkHealth` + HTTP+SSE）：

- 一个进程 = 全部会话：`{session_id: Agent}` 会话表，每会话独立的
  `Agent + AgentSession + Bus + AskPort + sandbox.Roots`（`Agent.__init__` 已保证）；
- 每会话一个 `asyncio` 任务跑 agent 循环（`asyncio.to_thread` 透传 ContextVar；
  裸 `threading.Thread` 禁止直接 `publish`，必须持 `bus` 显式调——`event/bus.py:16`）；
- 下行 `GET /events?session_id=&after_seq=`（SSE）：durable 重放 + volatile 直播，
  信封 `Envelope.to_record()` 直出（形状见 `packages/protocol/events.*`）；
- 上行 `POST /sessions/{id}/prompt|interrupt|dispatch` + `POST reply`；
- 管理面 `GET /workspace`（工作区信息）、`GET /commands`（命令表）、
  `GET /fs/list|/fs/read`（工作区内只读浏览，路径过文件工具的沙箱校验）、
  `POST /sessions/{id}/purge`（卸载 + 删日志）、`POST /sessions/{id}/rename`、
  `GET /sessions/{id}/skills`（会话级技能投影）、`/mcp/servers*`（MCP 面板：
  列表/日志/工具/启停/重连/授权/删除/添加）；
- 问询 `PromptStarted(id)` ─SSE─▶ 弹窗 ─`POST reply`─▶ `AskPort.reply()`，
  挂起快照 `GET /sessions/{id}/pending`（断线重连补发）。

只用标准库（`http.server.ThreadingHTTPServer` + chunked SSE）：项目惯例能标准库
就标准库；`mcp` 带的 starlette/uvicorn 只在测试里用，不转正。

安全：默认只绑回环；token 鉴权（显式参数 > `SMITH_AUTH_TOKEN` 环境变量 > 启动时
随机生成并打印一次）；来源白名单只放行 Tauri 与本地 dev（`SMITH_ALLOWED_ORIGINS`
可追加，见 `_DEFAULT_ORIGINS`）——只看 token 不够：浏览器不受同源策略约束，
绑回环也拦不住网页直连本地端口；workspace jail（每会话 `allowed_roots`）；
SSE 日志经 `mcp/secrets.redactor` 脱敏。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, sandbox, sessions
from .agent import Agent
from .event import activate as activate_bus
from .event import asks as asks_mod
from .event import reset as reset_bus
from .event.envelope import Envelope, payload_to_dict
from .session import Session
from .sessions import paths


class ServeError(Exception):
    """serve 侧错误：`message` 即 HTTP 错误体。"""


def _json_error(handler: BaseHTTPRequestHandler, *args) -> None:
    """发送 JSON 错误：默认 413 '请求体过大或不是 JSON 对象'，
    也接受 `_json_error(handler, status, message)`。"""

    def _send(status: int = 413, msg: str = "请求体过大或不是 JSON 对象") -> None:
        _send_json(handler, status, {"error": msg})

    if not args:
        _send()
    elif len(args) == 1:
        _send(args[0])
    else:
        _send(args[0], args[1])


def _json_body(handler: BaseHTTPRequestHandler, limit: int = 1 << 20) -> dict | None:
    """请求体解析：非法时返回 None（调用方回 400/413），不再吞成 `{}`。

    超限时仍尽量 drain（读到 limit 后丢弃）避免 broken pipe——客户端在
    收到响应前已发完 body，否则 close 就会撕 ClientRemoteDisconnected。
    """
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length < 0:
        return None
    if length > limit:
        # 超限：读 `limit` 字节丢弃，按 Content-Length 补吞多出的部分
        # （`read()` 无参读到 EOF 会阻塞 keep-alive 连接）
        with contextlib.suppress(OSError):
            handler.rfile.read(limit)
        remaining = length - limit
        if remaining > 0:
            with contextlib.suppress(OSError):
                handler.rfile.read(remaining)
        return None
    if length == 0:
        return {}
    try:
        raw = handler.rfile.read(length)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# 浏览器来源白名单：Tauri 打包后是 `tauri://localhost`（Windows 与移动端为
# `http://tauri.localhost`），dev 跑 vite 是 `http://localhost:1420`。
# 绑回环挡不住网页——浏览器会向 127.0.0.1 发跨站请求（simple request 连预检都省），
# 所以带 Origin 的请求必须逐一核对；ACAO 回显任意来源等于把本地服务开放给任意网页。
_DEFAULT_ORIGINS = (
    "tauri://localhost",
    "http://tauri.localhost",
    # `dangerousUseHttpScheme = false` 时 Windows 用 https，一并放行（仍是 Tauri 自有来源）
    "https://tauri.localhost",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
)


#: `/fs/list` 单次返回的最大条目数（目录可能很大，前端只做只读浏览）
_FS_LIST_MAX = 400

#: SSE 直播队列上限：慢消费者/断线 tab 不能让 serve 内存无限涨。volatile 事件
#: 可丢（重连靠重放 + `pending` 补），满了就丢最旧的 volatile 并记一个哨兵数；
#: durable 事件不经这里丢（它同时写了日志，重放能补回来）。
_LIVE_QUEUE_MAX = 512

#: SSE 重放上限：`after_seq=0` 全量重放的代价是 O(全部历史)，大会话重连一次
#: 就很贵。超限直接截断并回 `truncated: true` + 续跑游标，前端按游标继续拉。
_REPLAY_MAX = 500

#: SSE 直播空转轮询间隔：**首条事件的最坏延迟就是它**。本地 serve 连接数个位数，
#: 0.5s 一次空队列探测可忽略；按 idle 指数退避（老做法）会攒到 13s——模型思考的
#: 静默期过后，流的第一条增量要干等它睡醒。心跳仍按 15s 单独判（每轮都检查）。
_LIVE_POLL_INTERVAL = 0.5


def _allowed_origins() -> tuple[str, ...]:
    """默认白名单 + `SMITH_ALLOWED_ORIGINS`（逗号分隔）追加。"""
    extra = os.environ.get("SMITH_ALLOWED_ORIGINS", "")
    return _DEFAULT_ORIGINS + tuple(o.strip() for o in extra.split(",") if o.strip())


def _host_allowed(handler: BaseHTTPRequestHandler) -> bool:
    """Host 白名单：DNS-rebind（evil.com → 127.0.0.1）的请求没有可信 Origin，
    但 Host 一定是被攻击者控制的域名——只放行回环 host[:port]。
    无 Host 头（HTTP/1.0 老客户端/测试）放行，token 仍是第二道闸。"""
    host = (handler.headers.get("Host") or "").strip().lower()
    if not host:
        return True
    host = host.split(",")[0].strip()
    if host.startswith("["):
        name, _, rest = host[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        if name not in ("::1", "::ffff:127.0.0.1"):
            return False
    else:
        name, _, port = host.partition(":")
        if name not in ("127.0.0.1", "localhost"):
            return False
    return not port or port.isdigit()


def _origin_allowed(handler: BaseHTTPRequestHandler) -> bool:
    """有 Origin 必须在白名单内；无 Origin（curl/测试/非浏览器客户端）看 Host。"""
    origin = handler.headers.get("Origin")
    if origin:
        return origin in _allowed_origins()
    return _host_allowed(handler)


def _allow_origin(handler: BaseHTTPRequestHandler) -> None:
    """只对白名单来源回 ACAO（带 Vary），其余不回——由浏览器按默认策略拒绝读取。"""
    origin = handler.headers.get("Origin")
    if origin and origin in _allowed_origins():
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")


def resolve_auth_token(explicit: str = "") -> tuple[str, bool]:
    """解析 serve 鉴权 token：显式参数 > `SMITH_AUTH_TOKEN` > 随机生成。

    返回 `(token, 是否由调用方提供)`：只有随机生成的那份才需要打印给用户
    （sidecar 从 stdout 回读），环境变量/参数传入的不回显，避免密钥进日志。
    """
    provided = explicit or os.environ.get("SMITH_AUTH_TOKEN", "")
    if provided:
        return provided, True
    return secrets.token_hex(16), False


def _send_json(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _allow_origin(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _put_live(q: queue.Queue, env) -> None:
    """直播入队（有界）：满了丢最旧的 volatile，durable 不在这里丢。"""
    while True:
        try:
            q.put_nowait(env)
            return
        except queue.Full:
            pass
        try:
            oldest = q.get_nowait()
        except queue.Empty:
            continue
        if getattr(oldest, "durable", False):
            # 队里全是 durable（极端）：放回，退化成阻塞 put（durable 有日志兜底，
            # 但顺序不能乱——丢了它重放游标会断）
            try:
                q.put_nowait(oldest)
            except queue.Full:
                pass
            try:
                q.put(env, timeout=1)
            except queue.Full:
                pass
            return
        # 丢掉的就是一个 volatile：记数由消费侧的哨兵事件补（见 `_serve_events`）


class _SessionEntry:
    """一个在线会话：Agent + 事件订阅队列 + 循环绑定。"""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.bus = agent.events
        # 有界直播队列（_LIVE_QUEUE_MAX）：慢消费者不能把 serve 内存撑爆。
        # 订阅回调在总线线程里执行——必须非阻塞，否则整个事件层被一个慢会话拖住。
        self.live: queue.Queue = queue.Queue(maxsize=_LIVE_QUEUE_MAX)
        self._unsubscribe = self.bus.subscribe(lambda env: _put_live(self.live, env))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._task: asyncio.Task | None = None

    @property
    def id(self) -> str:
        return self.agent.session.id

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._unsubscribe()
        self.agent.close()

    def run_in_loop(self, coro):
        """把协程投到本会话的事件循环（serve 的 HTTP 线程调 agent 用）。"""
        with self._lock:
            loop = self._loop
        if loop is None or not loop.is_running():
            raise ServeError("会话循环未运行")
        return asyncio.run_coroutine_threadsafe(coro, loop)


class ServeState:
    """serve 进程状态：会话表 + 共享 MCP + 后台事件循环。"""

    def __init__(self, workspace=None, auth_token: str = "") -> None:
        self.workspace = workspace
        if workspace:
            # 启动参数与 config 对齐：`GET /workspace`、历史默认项目、新建会话落点
            # 三处同源，都读 `config.WORKSPACE_ROOT`（cli 走 serve 时也会先设一次，
            # 这里是幂等兜底——直接构造 ServeState 的宿主不必自己记得设）。
            config.set_workspace(workspace)
        self.auth_token, self.token_provided = resolve_auth_token(auth_token)
        self._lock = threading.Lock()
        self._entries: dict[str, _SessionEntry] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._mcp = None  # 进程级共享（连接复用，各会话注入）
        self._tickets: dict[str, float] = {}  # 一次性 SSE 票据（EventSource 发不了 header 用）

    # ---------- 进程生命周期 ----------

    def start(self) -> None:
        """启动后台事件循环（serve 进程内唯一的 agent 循环线程）。"""
        from . import (
            frontend as frontend_mod,  # 局部导入：serve 是宿主层，不得顶层依赖前端
        )

        frontend_mod.activate(frontend_mod.unavailable())  # serve 无 stdin：未装配的提问一律 fail-closed
        loop = asyncio.new_event_loop()
        self._loop = loop

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, name="smithcode-serve-loop", daemon=True)
        thread.start()
        self._loop_thread = thread

    def close(self) -> None:
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            with contextlib.suppress(Exception):
                entry.close()
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)

    # ---------- 会话管理 ----------

    def _new_agent(self, *, workspace: str | None = None,
                   extra: list[str] | None = None,
                   resume_target=None) -> Agent:
        # 会话级模型/思考强度/工作区：建会话时从 config 快照一份，
        # 各会话独立。`/model` `/effort` 只改自己的 Agent。
        # `extra` 传 None（而不是 `[]`）：`sandbox.fresh()` 才会照 config 的
        # `--add` 附加目录快照——传 `[]` 等于让 serve 会话静默丢掉附加授权目录。
        agent = Agent(Session(), persist=True, mcp=self.mcp_service(),
                      model=config.MODEL, effort=config.REASONING_EFFORT,
                      workspace=workspace, extra=extra,
                      reset_globals=False)
        agent.events.session_id = agent.session.id
        agent.asks.session_id = agent.session.id
        agent.asks.pending.session_id = agent.session.id
        agent.asks.set_remote_asker(object())  # serve 一律跨进程作答
        agent.session.bind_bus(agent.events)
        token = asks_mod.activate(agent.asks)
        # 装配期（skills/instructions 警告提示等）经 ContextVar 总线发的事件同样要
        # 进会话总线，否则在 start/resume 里被 bus 静默丢弃，前端永远看不到。
        bus_token = activate_bus(agent.events)
        try:
            agent.start()
            if resume_target is not None:
                agent.resume(resume_target)
        finally:
            asks_mod.reset(token)
            reset_bus(bus_token)
        return agent

    def create(self, *, workspace: str | None = None,
                 extra: list[str] | None = None,
                 resume_target=None) -> _SessionEntry:
        if resume_target is not None:
            # 目标仍在线：直接复用（前端刷新后重新打开同一个会话走这条路，
            # 再造一个 Agent 会变成两个写手往同一份日志里追加）
            with self._lock:
                existing = self._entries.get(resume_target)
            if existing is not None:
                return existing
            if workspace is None:
                # 跨项目恢复：沙箱跟会话走。不推导的话，从 A 打开 B 项目的会话
                # 会把 B 的文件挂到 A 的授权目录下，读写全部越界。
                workspace = self._workspace_for(resume_target)
        agent = self._new_agent(workspace=workspace, extra=extra,
                                resume_target=resume_target)
        entry = _SessionEntry(agent)
        entry._loop = self._loop
        agent.events.bind_loop(self._loop)
        agent.asks.bind_loop(self._loop)
        with self._lock:
            self._entries[entry.id] = entry
        return entry

    @staticmethod
    def _workspace_for(resume_target) -> str | None:
        """恢复目标所属的项目目录；定位不到就返回 None（回退默认沙箱）。

        报错不在这儿抛：目标解析的失败要等 `Agent.resume()` 才报（那时 Agent
        已建好，报错文案与 TUI 一致），这里只负责"能拿到就拿"。
        """
        try:
            summary = sessions.resolve_target(resume_target)
        except sessions.StoreError:
            return None
        return summary.cwd or None

    def get(self, session_id: str) -> _SessionEntry:
        with self._lock:
            entry = self._entries.get(session_id)
        if entry is None:
            raise ServeError(f"会话不存在: {session_id}")
        return entry

    def list_online(self) -> list[dict]:
        with self._lock:
            return [
                {"id": entry.id, "busy": entry.agent.busy,
                 "pending": len(entry.agent.asks.pending),
                 "permission": entry.agent.permission.mode,
                 # 会话有效值（非 last_turn）：idle 会话没跑过也有正确的显示
                 "model": entry.agent.effective_model(),
                 "effort": entry.agent.effective_effort(),
                 # 各会话的沙箱根：前端据此定位文件面板/项目 chip（不是进程默认）
                 "workspace": str(entry.agent.roots.workspace)}
                for entry in self._entries.values()
            ]

    def drop(self, session_id: str) -> bool:
        with self._lock:
            entry = self._entries.pop(session_id, None)
        if entry is None:
            return False
        entry.close()
        return True

    # ---------- 面板与管理入口 ----------

    def mcp_service(self):
        """进程级共享的 MCP 服务（懒加载；没有会话时面板也能用）。"""
        from .mcp.service import McpService

        with self._lock:
            if self._mcp is None:
                self._mcp = McpService()
                self._mcp.start()
            return self._mcp

    def start_task(self, session_id: str, text: str) -> None:
        """不等待地起一轮任务（技能加载、目标续跑等命令的服务端续跑）。"""
        entry = self.get(session_id)
        future = entry.run_in_loop(entry.agent.prompt(text))

        def _log_error(done) -> None:
            with contextlib.suppress(Exception):
                exc = done.exception()
                if exc is not None:
                    print(f"[serve] 后台任务异常: {exc!r}", flush=True)

        future.add_done_callback(_log_error)

    def purge(self, session_id: str) -> bool:
        """真删：先从内存卸载（Windows 下要先关文件句柄），再删磁盘日志。"""
        self.drop(session_id)
        return sessions.delete(session_id)

    def rename(self, session_id: str, title: str) -> bool:
        """改标题：在线会话发标题事件（SSE 直达前端），离线会话追加到日志。"""
        try:
            entry = self.get(session_id)
        except ServeError:
            return sessions.rename(session_id, title)
        entry.agent.session.set_title(title, source="user")
        return True

    def workspace_info(self) -> dict:
        """默认工作区信息（Folders chip、关于页共用）。"""
        root = Path(config.WORKSPACE_ROOT).resolve()  # 与注册表/会话 cwd 同为已解析路径
        return {
            "cwd": str(root),
            "name": root.name or str(root),
            "extra": [str(Path(p)) for p in config.EXTRA_ROOTS],
        }

    def workspace_info_of(self, entry: _SessionEntry) -> dict:
        """单会话的工作区信息（前端项目切换器用）。"""
        roots = entry.agent.roots
        cwd = roots.workspace
        return {"cwd": str(cwd), "name": Path(cwd).name or str(cwd),
                "extra": [str(Path(p)) for p in roots.extra]}

    def list_projects(self) -> dict:
        """全部项目：slug / 目录 / 显示名 / 会话数 / 最近更新（注册表 + 扫盘合并）。

        注册表打底——「打开目录…」刚登记、还没落会话的项目也要看得见；
        扫盘补会话数与更新时间，注册表没命中的存量项目从日志回填 cwd。
        """
        table: dict[str, dict] = {}
        for slug, meta in paths.read_registry().items():
            if not isinstance(meta, dict):
                continue
            cwd = str(meta.get("cwd") or "")
            table[slug] = {
                "slug": slug,
                "cwd": cwd,
                "name": str(meta.get("name") or (Path(cwd).name if cwd else slug)),
                "sessionCount": 0,
                "updated": float(meta.get("created") or 0.0),
            }
        root = paths.projects_dir()
        if root.is_dir():
            for slug_dir in root.iterdir():
                if not slug_dir.is_dir():
                    continue
                registered = slug_dir.name in table
                sess_dir = slug_dir / "sessions"
                count = 0
                updated = 0.0
                for p in sess_dir.glob("*.jsonl"):
                    count += 1
                    try:
                        m = p.stat().st_mtime
                    except OSError:
                        continue
                    updated = max(updated, m)
                if not count and not registered:
                    continue  # 既没会话也没登记的空壳目录（删过会话的残留）
                item = table.get(slug_dir.name)
                if item is None:
                    item = {"slug": slug_dir.name, "cwd": "", "name": slug_dir.name,
                            "sessionCount": 0, "updated": 0.0}
                    table[slug_dir.name] = item
                if count:
                    item["sessionCount"] = count
                    item["updated"] = max(float(item["updated"]), updated)
                if not item["cwd"]:
                    item["cwd"] = sessions.resolve_project(slug_dir.name)
                    if item["cwd"]:
                        item["name"] = Path(item["cwd"]).name or item["slug"]
        # 当前默认工作区打底：刚启动、既没会话也没登记的项目也要出现在列表里，
        # 否则侧栏按 cwd 反查不到 slug（高亮对不上），用户也切不出去。
        default_cwd = str(Path(config.WORKSPACE_ROOT).resolve())
        table.setdefault(paths.project_slug(default_cwd), {
            "slug": paths.project_slug(default_cwd),
            "cwd": default_cwd,
            "name": Path(default_cwd).name or default_cwd,
            "sessionCount": 0,
            "updated": 0.0,
        })
        projects = list(table.values())
        projects.sort(key=lambda p: p["updated"], reverse=True)
        return {"projects": projects}

    def get_entry(self, session_id: str) -> _SessionEntry:
        with self._lock:
            entry = self._entries.get(session_id)
        if entry is None:
            raise ServeError(f"会话不存在: {session_id}")
        return entry

    # ---------- 任务入口 ----------

    def prompt(self, session_id: str, text: str, *, timeout: float | None = 600):
        """提交一轮任务：空闲即开跑，运行中按队列配置入队（与 `Agent.prompt` 一致）。

        同步等回执：默认最多等 600s（HTTP 线程是有限的，长任务请走 `prompt_async`）。
        """
        entry = self.get(session_id)
        tok = sandbox.activate(entry.agent.roots)
        try:
            future = entry.run_in_loop(entry.agent.prompt(text))
            result = future.result(timeout=timeout)
        finally:
            sandbox.reset(tok)
        if result is None:
            return {"queued": True}
        return {"queued": False, "status": result.status, "text": result.text}

    def prompt_async(self, session_id: str, text: str):
        """异步提交（对齐 opencode `prompt_async`）：202 直接回，不抱 HTTP 线程。"""
        entry = self.get(session_id)
        tok = sandbox.activate(entry.agent.roots)
        try:
            future = entry.run_in_loop(entry.agent.prompt(text))
        finally:
            sandbox.reset(tok)

        def _log_error(done) -> None:
            with contextlib.suppress(Exception):
                exc = done.exception()
                if exc is not None:
                    print(f"[serve] 后台任务异常: {exc!r}", flush=True)

        future.add_done_callback(_log_error)
        return {"queued": True}

    # ---------- 一次性 SSE 票据 ----------

    def issue_ticket(self, ttl: float = 30) -> str:
        """签发一次性 SSE 票据：`EventSource` 发不了 `Authorization` header，
        长效 token 又不能进 URL——前端先 `POST /events/ticket`（header 鉴权）
        换一张 30s 有效、一次有效的票据，再拼进 `?ticket=` 建 SSE 连接。"""
        import time

        ticket = secrets.token_hex(16)
        with self._lock:
            now = time.time()
            self._tickets = {t: exp for t, exp in self._tickets.items() if exp > now}
            self._tickets[ticket] = now + ttl
        return ticket

    def consume_ticket(self, ticket: str) -> bool:
        """消费票据：命中即删（重放无效），过期即删。"""
        import time

        with self._lock:
            exp = self._tickets.pop(ticket, None)
        return exp is not None and exp > time.time()

    def rotate_token(self) -> str:
        """轮换鉴权 token：泄漏后不用重启 serve，前端拿新 token 重连即可。"""
        with self._lock:
            self.auth_token = secrets.token_hex(16)
            self.token_provided = True
            self._tickets.clear()
            return self.auth_token

    # ---------- 排队项管理 ----------

    def take_queued(self, session_id: str, item_id: str):
        """取回编辑：出队并把文本还给前端填回输入框（对齐 TUI `edit_queued`）。"""
        entry = self.get(session_id)
        item = entry.agent.take_queued(item_id)
        if item is None:
            raise ServeError(f"排队项不存在: {item_id}")
        return {"id": item.id, "text": item.text}

    def cancel_queued(self, session_id: str, item_id: str) -> bool:
        """撤销一条排队输入（对齐 TUI 行尾 ✕）。"""
        entry = self.get(session_id)
        return entry.agent.cancel_queued(item_id)


def _redact(record: dict) -> dict:
    """SSE 记录脱敏（凭据/密钥不落前端日志）。

    登记在全局脱敏器里的值可能藏在任意层级的字符串里（工具结果、命令文本、
    MCP 错误），所以按容器递归过一遍 `scrub`，而不是只处理顶层字符串。
    """
    try:
        from .mcp.secrets import redactor

        scrub = redactor().scrub

        def walk(value):
            if isinstance(value, str):
                return scrub(value)
            if isinstance(value, dict):
                return {key: walk(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [walk(item) for item in value]
            return value

        return walk(record)
    except Exception:  # noqa: BLE001 脱敏失败时原样发送，不阻断事件
        return record


def _json_fallback(value):
    """JSON 兜底：不可序列化的字段降级成类型标记，一条事件不该打断整条 SSE。

    易失事件允许携带进程内对象（声明侧只对 durable 事件查形状），SSE 却要
    把**所有**事件都写成 JSON——发布侧一旦传错（如 owner 塞了实例）就地抛
    TypeError，整个连接的后续事件全丢。只留类型名、不给实例内容：兜底发生在
    `_redact` 之后，`str(value)` 里若藏凭据就没被脱敏过。
    """
    try:
        cls = type(value)
        return f"<{cls.__module__}.{cls.__qualname__}>"
    except Exception:  # noqa: BLE001 连类型都取不到也别让流挂掉
        return "<unknown>"


def _fs_list(rel: str) -> dict:
    """**当前沙箱**内的目录列表（只读）：路径解析走文件工具的沙箱校验。

    「当前沙箱」= HTTP 线程上 `sandbox.activate()` 挂的那份（会话级路由挂
    `entry.agent.roots`，兜底路由挂进程默认），见 `_serve_fs`。
    """
    from .tools import files as files_tool
    from .tools._shared_local import SKIP_DIRS

    target = files_tool._resolve(rel or ".")
    if not target.is_dir():
        raise NotADirectoryError(f"不是目录: {rel}")
    dirs: list[dict] = []
    others: list[dict] = []
    truncated = False
    try:
        children = sorted(target.iterdir(), key=lambda p: p.name.lower())
    except OSError as e:
        raise FileNotFoundError(f"无法读取目录: {e}") from e
    for child in children:
        if child.name in SKIP_DIRS:
            continue
        try:
            stat = child.lstat()
        except OSError:
            continue
        if child.is_dir():
            dirs.append({"name": child.name, "type": "dir", "size": 0})
        elif child.is_file():
            others.append({"name": child.name, "type": "file", "size": stat.st_size})
    entries = dirs + others
    if len(entries) > _FS_LIST_MAX:
        entries = entries[:_FS_LIST_MAX]
        truncated = True
    return {"path": _normalize_rel(rel), "entries": entries, "truncated": truncated}


def _fs_read(rel: str) -> dict:
    """**当前沙箱**内的文件文本预览（只读；二进制拒读、超限截断）。"""
    from . import textfile
    from .context.meter import truncate_output
    from .tools import files as files_tool

    target = files_tool._resolve(rel)
    if not target.exists():
        raise FileNotFoundError(f"文件不存在: {rel}")
    if target.is_dir():
        raise IsADirectoryError(f"是目录而不是文件: {rel}")
    if files_tool._is_binary(target):
        raise ValueError(f"二进制文件不支持预览: {rel}")
    try:
        text, _fmt = textfile.read(target)
    except textfile.TextFileError as e:
        raise ValueError(f"不是 UTF-8 文本: {e}") from e
    size = len(text)
    truncated = False
    limit = config.MAX_TOOL_OUTPUT
    if limit and len(text) > limit:
        text = truncate_output(text, limit)
        truncated = True
    return {"content": text, "truncated": truncated, "size": size}


def _normalize_rel(rel: str) -> str:
    """相对路径规范化：`""` / `"."` / `"./a/"` → `""` / `"a"`（面包屑直接用）。"""
    text = (rel or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    text = text.strip("/")
    return "" if text == "." else text


def _serve_fs(handler, query, kind: str) -> None:
    """文件面板分发：在**当前沙箱**里解析路径，异常映射成 403/404/400。

    沙箱由调用方挂（会话级路由挂 `entry.agent.roots`，兜底路由用进程默认），
    这里只负责取参数、调用与错误码。
    """
    rel = (query.get("path") or ["." if kind == "list" else ""])[0]
    try:
        payload = _fs_list(rel) if kind == "list" else _fs_read(rel)
    except PermissionError as e:
        _send_json(handler, 403, {"error": str(e)})
        return
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as e:
        _send_json(handler, 404, {"error": str(e)})
        return
    except ValueError as e:
        _send_json(handler, 400, {"error": str(e)})
        return
    _send_json(handler, 200, payload)


def _resolve_project_root(path: str) -> Path:
    """校验并解析项目根目录（存在 + resolve 符号链接）。"""
    p = Path(path).resolve()
    if not p.is_dir():
        raise ValueError(f"目录不存在: {p}")
    return p


def _slug_for(cwd: str) -> str:
    return paths.project_slug(cwd)


def _server_status_dict(status) -> dict:
    """MCP `ServerStatus` → JSON（命令/地址/报错都过脱敏：可能夹带展开后的密钥）。"""
    from .mcp.secrets import redactor

    scrub = redactor().scrub
    return {
        "name": status.name,
        "scope": status.scope,
        "scope_label": status.scope_label,
        "state": status.state,
        "state_label": status.state_label,
        "target": scrub(status.command),
        "transport": status.transport,
        "url": scrub(status.url),
        "oauth": status.oauth,
        "tool_count": status.tool_count,
        "error": scrub(status.error),
        "missing": list(status.missing),
    }


def _skill_dict(skill, active: bool) -> dict:
    """技能 → JSON。正文只在发现时读过一次（`skill.body`），这里不再碰磁盘。"""
    from .context.meter import truncate_output
    from .skills.registry import list_resources

    body = skill.body or ""
    limit = config.MAX_TOOL_OUTPUT
    if limit and len(body) > limit:
        body = truncate_output(body, limit)
    resources = list_resources(skill.base)
    return {
        "name": skill.name,
        "description": skill.description,
        "scope": skill.scope,
        "location": str(skill.location),
        "active": active,
        "disabled": bool(skill.disabled),
        "disabled_by": getattr(skill, "disabled_by", ""),
        "model_invocable": bool(skill.model_invocable),
        "warnings": list(getattr(skill, "warnings", [])),
        "resources": resources,
        "body": body,
    }


def _server_config_from(body: dict):
    """面板表单 → `ServerConfig`（与 `/mcp add` 直通路径同一套校验）。"""
    from .mcp.config import ServerConfig
    from .mcp.errors import McpConfigError

    name = str(body.get("name") or "").strip()
    kind = str(body.get("type") or "stdio").strip()
    if not name:
        raise McpConfigError("服务器名不能为空")
    if kind not in ("stdio", "http", "sse"):
        raise McpConfigError(f"不支持的传输类型: {kind}")
    if kind == "stdio":
        raw = body.get("command") or []
        command = [str(item) for item in raw if str(item).strip()] if isinstance(raw, list) else []
        if not command:
            raise McpConfigError("stdio 服务器需要命令（argv 数组）")
        env = body.get("env") if isinstance(body.get("env"), dict) else {}
        return ServerConfig(
            name=name, type=kind, command=command,
            env={str(k): str(v) for k, v in env.items()},
            cwd=str(body.get("cwd") or ""),
        )
    url = str(body.get("url") or "").strip()
    if not url:
        raise McpConfigError("远程服务器需要 url")
    headers = body.get("headers") if isinstance(body.get("headers"), dict) else {}
    return ServerConfig(
        name=name, type=kind, url=url, oauth=bool(body.get("oauth")),
        headers={str(k): str(v) for k, v in headers.items()},
    )


def _skills_payload(entry) -> dict:
    """会话级技能状态。

    **必须读会话持有的缓存实例**（`entry.agent.session_owner.skills_state`）：
    `skills.*` 模块函数按 ContextVar 解析，HTTP 线程上会落到进程默认实例并
    触发一次全盘扫描（非交互下项目技能还会 fail-closed 跳过）。
    """
    state = entry.agent.session_owner.skills_state
    skills = [_skill_dict(skill, skill.name in state.active) for skill in state.skills]
    return {"loaded": state.loaded, "skills": skills, "diagnostics": list(state.diagnostics)}


class _Handler(BaseHTTPRequestHandler):
    """serve 的 HTTP 面：REST + SSE。路由按前缀匹配，`state` 挂在 server 上。"""

    @property
    def server_version(self) -> str:
        from . import __version__

        return f"SmithServe/{__version__}"

    @property
    def state(self) -> ServeState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, *args) -> None:  # 安静：serve 日志由调用方接管
        pass

    def _check_origin(self) -> bool:
        """来源白名单：不在名单内直接 403，不动任何状态。

        serve 关掉了访问日志（`log_message`），而白名单打错会让整个前端 403——
        所以这里显式喊一声，否则只能对着一片 401/403 猜。
        """
        if _origin_allowed(self):
            return True
        origin = self.headers.get("Origin")
        host = self.headers.get("Host")
        print(
            f"[serve] 来源被拒: origin={origin!r} host={host!r}"
            f"（白名单: {' / '.join(_allowed_origins())}；"
            f"追加用 SMITH_ALLOWED_ORIGINS）",
            flush=True,
        )
        _send_json(self, 403, {"error": "来源不被允许"})
        return False

    def _auth(self) -> bool:
        token = self.state.auth_token
        if not token:
            return True
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            return secrets.compare_digest(header[7:], token)
        # 一次性 SSE 票据（`?ticket=`）：POST /events/ticket 签发、一次有效、
        # 30s 过期。长效 token 永不进 URL（日志/历史/dump 全收）。
        query = parse_qs(urlparse(self.path).query)
        for ticket in query.get("ticket", []):
            if self.state.consume_ticket(ticket):
                return True
        return False

    def _route(self) -> tuple[str, str, dict]:
        parsed = urlparse(self.path)
        return self.command, parsed.path, parse_qs(parsed.query)

    def do_GET(self) -> None:
        if not self._check_origin():
            return
        _, path, query = self._route()
        parts = path.strip("/").split("/")
        if path == "/health":
            from . import __version__

            _send_json(self, 200, {"ok": True, "version": __version__})
            return
        if not self._auth():
            _send_json(self, 401, {"error": "未授权"})
            return
        if path == "/workspace":
            _send_json(self, 200, self.state.workspace_info())
            return
        if path == "/projects":
            _send_json(self, 200, self.state.list_projects())
            return
        if path == "/commands":
            from .commands import all_commands

            _send_json(self, 200, {"commands": [
                {"name": cmd.name, "description": cmd.description,
                 "usage": cmd.usage or f"/{cmd.name}"}
                for cmd in all_commands()
            ]})
            return
        if path == "/sessions":
            _send_json(self, 200, {"sessions": self.state.list_online()})
            return
        if path == "/sessions/history":
            try:
                limit = int((query.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            limit = max(1, min(limit, 200))
            want_all = (query.get("all") or [""])[0].lower() in ("1", "true", "yes")
            project = (query.get("project") or [""])[0] or None
            if want_all:
                summaries = sessions.list_all_sessions(limit=limit)
            elif project:
                # slug 不可逆：经注册表/日志回填成目录再列，而不是拿 slug 去比对
                cwd = sessions.resolve_project(project)
                if not cwd:
                    _send_json(self, 404, {"error": f"未知项目: {project}"})
                    return
                summaries = sessions.list_sessions(cwd=cwd, limit=limit)
            else:
                summaries = sessions.list_sessions(limit=limit)  # 默认：当前项目
            items = [
                {"id": s.id, "title": s.title, "updated": s.updated,
                 "created": s.created, "cwd": s.cwd,
                 "model": s.model, "first_prompt": s.first_prompt}
                for s in summaries
            ]
            _send_json(self, 200, {"sessions": items})
            return
        if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "fs" \
                and parts[3] in ("list", "read"):
            # 会话级文件面板：沙箱挂这一份，多项目下才不会串到别人的目录。
            # 解析走 `files._resolve`（读 `sandbox.current()`），越界 403。
            try:
                entry = self.state.get(parts[1])
            except ServeError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            tok = sandbox.activate(entry.agent.roots)
            try:
                _serve_fs(self, query, parts[3])
            finally:
                sandbox.reset(tok)
            return
        if path == "/fs/list":
            # 进程默认沙箱的兜底（没有会话上下文时的老客户端/调试用）
            _serve_fs(self, query, "list")
            return
        if path == "/fs/read":
            _serve_fs(self, query, "read")
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "workspace":
            try:
                entry = self.state.get(parts[1])
            except ServeError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            _send_json(self, 200, self.state.workspace_info_of(entry))
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "skills":
            try:
                entry = self.state.get(parts[1])
            except ServeError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            _send_json(self, 200, _skills_payload(entry))
            return
        if path == "/mcp/servers":
            try:
                servers = [_server_status_dict(s) for s in self.state.mcp_service().status()]
            except Exception as e:  # noqa: BLE001 MCP 子系统故障不该拖垮整个 HTTP 面
                _send_json(self, 500, {"error": f"{type(e).__name__}: {e}"})
                return
            _send_json(self, 200, {"servers": servers})
            return
        if (len(parts) == 4 and parts[0] == "mcp" and parts[1] == "servers"
                and parts[3] in ("logs", "tools")):
            from .mcp.secrets import redactor

            name = parts[2]
            service = self.state.mcp_service()
            if service.get(name) is None:
                _send_json(self, 404, {"error": f"未配置的 MCP 服务器: {name}"})
                return
            if parts[3] == "logs":
                try:
                    lines = int((query.get("lines") or ["20"])[0])
                except ValueError:
                    lines = 20
                _send_json(self, 200, {
                    "logs": redactor().scrub(service.logs(name, lines=max(1, min(lines, 200)))),
                })
                return
            tools = [
                {
                    "original": str(item.get("original", "")),
                    "exposed": str(item.get("exposed", "")),
                    "description": redactor().scrub(str(item.get("description", ""))),
                    "read_only": bool(item.get("read_only")),
                }
                for item in service.tools(name)
            ]
            _send_json(self, 200, {"tools": tools})
            return
        if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "pending":
            try:
                entry = self.state.get(parts[1])
            except ServeError as e:
                _send_json(self, 404, {"error": str(e)})
                return
            prompts = [payload_to_dict(p) for p in entry.agent.asks.pending_snapshot()]
            _send_json(self, 200, {"pending": prompts})
            return
        if path == "/events":
            self._serve_events(query)
            return
        _send_json(self, 404, {"error": f"未知路径: {path}"})

    def do_POST(self) -> None:
        if not self._check_origin():
            return
        _, path, _ = self._route()
        if not self._auth():
            _send_json(self, 401, {"error": "未授权"})
            return
        body = _json_body(self)
        if body is None:
            _json_error(self)
            return
        try:
            if path == "/events/ticket":
                _send_json(self, 200, {"ticket": self.state.issue_ticket()})
                return
            if path == "/auth/rotate":
                _send_json(self, 200, {"token": self.state.rotate_token()})
                return
            if path == "/projects":
                # 登记一个项目目录（「打开目录…」）：只写注册表，不碰全局 config
                raw = str(body.get("path") or "").strip()
                if not raw:
                    _json_error(self, 400, "缺少 path")
                    return
                try:
                    target = _resolve_project_root(raw)
                except ValueError as exc:
                    _json_error(self, 400, str(exc))
                    return
                slug = paths.touch_project(target)
                _send_json(self, 200, {"slug": slug, "cwd": str(target),
                                        "name": target.name or str(target)})
                return
            if path == "/workspace":
                # 切项目：改进程默认（新建落点 / GET /workspace / 历史默认项目），
                # 已建会话不受影响——它们在 sandbox.fresh() 时就快照了各自目录。
                raw = str(body.get("path") or "").strip()
                if not raw:
                    _json_error(self, 400, "缺少 path")
                    return
                try:
                    target = _resolve_project_root(raw)
                except ValueError as exc:
                    _json_error(self, 400, str(exc))
                    return
                config.set_workspace(target)
                _send_json(self, 200, {"ok": True, **self.state.workspace_info()})
                return
            if path == "/sessions":
                ws = body.get("workspace") or None
                ex = body.get("extra") or None
                if ws is not None:
                    try:
                        p = _resolve_project_root(ws)
                    except ValueError as exc:
                        _json_error(self, 400, str(exc))
                        return
                    ws = str(p)
                resume_target = body.get("resume") or None
                entry = self.state.create(workspace=ws, extra=ex,
                                            resume_target=resume_target)
                _send_json(self, 200, {"id": entry.id})
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "prompt":
                try:
                    result = self.state.prompt(parts[1], str(body.get("text") or ""))
                except concurrent.futures.TimeoutError:
                    _send_json(self, 202, {"queued": True, "timeout": True})
                    return
                _send_json(self, 200, result)
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "prompt_async":
                entry = self.state.get(parts[1])
                if not str(body.get("text") or "").strip():
                    _send_json(self, 400, {"error": "内容不能为空"})
                    return
                _send_json(self, 202, self.state.prompt_async(parts[1], str(body.get("text") or "")))
                return
            if len(parts) == 4 and parts[0] == "sessions" and parts[2] == "queue":
                if parts[3] == "take":
                    item_id = str(body.get("id") or "").strip()
                    if not item_id:
                        _send_json(self, 400, {"error": "缺少排队项 id"})
                        return
                    _send_json(self, 200, self.state.take_queued(parts[1], item_id))
                    return
                if parts[3] == "cancel":
                    item_id = str(body.get("id") or "").strip()
                    if not item_id:
                        _send_json(self, 400, {"error": "缺少排队项 id"})
                        return
                    _send_json(self, 200, {"ok": self.state.cancel_queued(parts[1], item_id)})
                    return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "interrupt":
                entry = self.state.get(parts[1])
                entry.agent.interrupt()
                _send_json(self, 200, {"ok": True})
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "purge":
                ok = self.state.purge(parts[1])
                if not ok:
                    _send_json(self, 404, {"error": f"会话不存在: {parts[1]}"})
                    return
                _send_json(self, 200, {"ok": True})
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "rename":
                title = str(body.get("title") or "").strip()
                if not title:
                    _send_json(self, 400, {"error": "标题不能为空"})
                    return
                _send_json(self, 200, {"ok": self.state.rename(parts[1], title)})
                return
            if len(parts) == 3 and parts[0] == "sessions" and parts[2] == "dispatch":
                entry = self.state.get(parts[1])
                from . import commands
                from .commands.base import CommandSelect, CommandWizard

                result = commands.dispatch(
                    entry.agent, str(body.get("text") or ""), interactive=False
                )
                payload: dict = {"text": result.text, "kind": result.kind}
                if result.select is not None and isinstance(result.select, CommandSelect):
                    payload["select"] = {
                        "title": result.select.title,
                        "command": result.select.command,
                        "readonly": result.select.readonly,
                        "items": [
                            {"label": i.label, "value": i.value, "current": i.current,
                             "description": i.description, "trailing": i.trailing}
                            for i in result.select.items if not i.separator
                        ],
                    }
                if result.wizard is not None and isinstance(result.wizard, CommandWizard):
                    payload["wizard"] = {
                        "name": result.wizard.name, "payload": result.wizard.payload,
                    }
                for flag in ("session_reset", "session_resume", "refresh_status",
                             "start_compact"):
                    value = getattr(result, flag, None)
                    if value:
                        payload[flag] = value
                if result.inject_history or result.start_task is not None:
                    # 技能载荷注入与续跑都在服务端完成（与 TUI 同一套语义）：注入必须
                    # 先于开跑落库，否则历史里只有任务没有指令；busy 时两者一起跳过，
                    # 不留孤儿消息（与 TUI 的 busy 守卫一致）。
                    if entry.agent.busy:
                        payload["skipped_busy"] = True
                    else:
                        for role, content in (result.inject_history or []):
                            entry.agent.session.add(role, content)
                        if result.start_task is not None:
                            self.state.start_task(parts[1], result.start_task)
                _send_json(self, 200, payload)
                return
            if (len(parts) == 5 and parts[0] == "sessions" and parts[2] == "prompts"
                    and parts[4] == "reply"):
                entry = self.state.get(parts[1])
                from .event.asks import AskAnswer

                outcome = str(body.get("outcome") or "cancelled")
                answer = AskAnswer(
                    outcome=outcome if outcome in ("answered", "cancelled", "denied", "error") else "cancelled",
                    value=str(body.get("value") or ""),
                    values=tuple(body.get("values") or ()),
                )
                ok = entry.agent.asks.reply(parts[3], answer)
                _send_json(self, 200, {"ok": ok})
                return
            if (len(parts) == 3 and parts[0] == "sessions"
                    and parts[2] == "permission"):
                # 权限模式切换（Permission.mode）：smith / accept_edits / auto。
                # 非法值 400，不动。返回当前值供状态栏确认。
                entry = self.state.get(parts[1])
                from .permission.engine import MODES

                mode = str(body.get("mode") or "")
                if mode not in MODES:
                    _send_json(self, 400, {"error": f"非法权限模式: {mode!r}（可选 {' / '.join(MODES)}）"})
                    return
                entry.agent.permission.mode = mode
                _send_json(self, 200, {"mode": entry.agent.permission.mode})
                return
            if (len(parts) == 2 and parts[0] == "sessions"
                    and str(body.get("method") or "").upper() == "DELETE"):
                # DELETE 语义经 POST 方法重载（部分 SSE 客户端发不出 DELETE）
                ok = self.state.drop(parts[1])
                if not ok:
                    _send_json(self, 404, {"error": f"会话不存在: {parts[1]}"})
                    return
                _send_json(self, 200, {"ok": True})
                return
            if path == "/mcp/servers":
                from .mcp.errors import McpConfigError

                scope = str(body.get("scope") or "user").strip()
                if scope not in ("user", "project"):
                    _send_json(self, 400, {"error": f"作用域只能是 user / project，收到 {scope!r}"})
                    return
                try:
                    cfg = _server_config_from(body)
                    status = self.state.mcp_service().add(cfg, scope=scope)
                except McpConfigError as e:
                    _send_json(self, 400, {"error": str(e)})
                    return
                except Exception as e:  # noqa: BLE001 写盘失败/连接异常按输入问题处理，不暴露 traceback
                    _send_json(self, 400, {"error": f"{type(e).__name__}: {e}"})
                    return
                _send_json(self, 200, {"ok": True, "server": _server_status_dict(status)})
                return
            if (len(parts) == 4 and parts[0] == "mcp" and parts[1] == "servers"
                    and parts[3] in ("enable", "disable", "reconnect", "auth", "remove")):
                name = parts[2]
                service = self.state.mcp_service()
                if service.get(name) is None:
                    _send_json(self, 404, {"error": f"未配置的 MCP 服务器: {name}"})
                    return
                action = parts[3]
                if action == "enable":
                    ok = service.set_enabled(name, True)
                elif action == "disable":
                    ok = service.set_enabled(name, False)
                elif action == "reconnect":
                    ok = service.reconnect(name)
                elif action == "auth":
                    ok = service.authorize(name)
                else:
                    ok = service.remove(name)
                _send_json(self, 200, {"ok": bool(ok)})
                return
            if path == "/settings/reload":
                changed = config.reload()
                _send_json(self, 200, {"changed": {k: list(v) for k, v in changed.items()}})
                return
            if path == "/settings/provider":
                from . import settings

                try:
                    changed = settings.apply_provider(
                        url=str(body.get("url") or ""),
                        model=str(body.get("model") or ""),
                        key=str(body.get("key") or ""),
                        budget=int(body.get("budget") or 0),
                    )
                except OSError as e:
                    _send_json(self, 500, {"error": f"写入失败: {e}"})
                    return
                _send_json(self, 200, {"changed": {k: list(v) for k, v in changed.items()}})
                return
            if path == "/settings":
                from . import settings

                _send_json(self, 200, settings.snapshot())
                return
        except ServeError as e:
            _send_json(self, 404, {"error": str(e)})
            return
        except Exception as e:  # noqa: BLE001 未预期异常转 500，不暴露 traceback
            _send_json(self, 500, {"error": f"{type(e).__name__}: {e}"})
            return
        _send_json(self, 404, {"error": f"未知路径: {path}"})

    def do_DELETE(self) -> None:
        if not self._check_origin():
            return
        _, path, _ = self._route()
        if not self._auth():
            _send_json(self, 401, {"error": "未授权"})
            return
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "sessions":
            ok = self.state.drop(parts[1])
            if not ok:
                _send_json(self, 404, {"error": f"会话不存在: {parts[1]}"})
                return
            _send_json(self, 200, {"ok": True})
            return
        _send_json(self, 404, {"error": f"未知路径: {path}"})

    def do_OPTIONS(self) -> None:
        # 预检同样过白名单：否则非白名单来源可以先拿到"允许带 Authorization"的答复，
        # 再拿偷来的 token 直接打。CORS 只在来源合法时才作数，这是浏览器侧的硬性要求。
        if not _origin_allowed(self):
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        _allow_origin(self)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()

    # ---------- SSE ----------

    def _serve_events(self, query: dict) -> None:
        session_ids = query.get("session_id", [])
        # 按会话游标：`after_seq`（兼容旧前端，全会话共用）+ `cursors`（新前端，
        # `{"sid": seq}` JSON，谁传了谁生效）。单游标多会话共用时，开旧会话会把
        # 所有会话全量重放一遍——按会话记游标是正解（见桌面端 useSse）。
        cursors: dict[str, int] = {}
        raw_cursors = (query.get("cursors") or [""])[0]
        if raw_cursors:
            with contextlib.suppress(ValueError):
                parsed = json.loads(raw_cursors)
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        if isinstance(value, int) and value >= 0:
                            cursors[str(key)] = value
        after_seq = 0
        raw_after = (query.get("after_seq") or ["0"])[0]
        try:
            after_seq = max(0, int(raw_after))
        except ValueError:
            _send_json(self, 400, {"error": f"after_seq 不是整数: {raw_after!r}"})
            return
        try:
            limit = int((query.get("limit") or [str(_REPLAY_MAX)])[0])
        except ValueError:
            limit = _REPLAY_MAX
        limit = max(1, min(limit, _REPLAY_MAX))
        entries = []
        for sid in session_ids:
            try:
                entries.append(self.state.get(sid))
            except ServeError:
                pass
        if not entries:
            _send_json(self, 404, {"error": "会话不存在或未指定 session_id"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        _allow_origin(self)
        self.end_headers()

        def _write(record: dict) -> bool:
            try:
                # default=_json_fallback：字段不可 JSON 化时降级而非炸线程
                payload = json.dumps(_redact(record), ensure_ascii=False,
                                     default=_json_fallback)
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                return True
            except (OSError, ValueError, TypeError):
                # TypeError 兜漏网的非字符串键：停这条流（前端按游标重连）
                # 好过把 handler 线程炸掉、连接僵在半截
                return False

        # durable 重放：各会话按自己的游标补（volatile 不重放，可丢）。
        # 超 _REPLAY_MAX 截断：回 `truncated` + 续跑游标，前端按游标继续拉
        # （opencode 那边历史走分页接口、SSE 只推增量；这里先给上限 + 续跑，
        # 不改前端也能把最坏情况封顶）。
        truncated: dict[str, int] = {}
        for entry in entries:
            cursor = cursors.get(entry.id, after_seq)
            sent = 0
            last_seq = cursor
            with contextlib.suppress(Exception):
                summary = sessions.find(entry.id)
                if summary is None:
                    continue
                loaded = sessions.load(summary)
                for env in loaded.events:
                    if not isinstance(env, Envelope) or (env.seq or 0) <= cursor:
                        continue
                    last_seq = max(last_seq, env.seq or 0)
                    if sent >= limit:
                        truncated[entry.id] = last_seq
                        break
                    if not _write(env.to_record()):
                        return
                    sent += 1
        if truncated and not _write({"type": "replay.truncated", "truncated": truncated}):
            return
        # 直播：订阅队列直出（多会话短超时轮询 + 15s 心跳）。
        # durable 事件也走这里——订阅者拿到的信封 seq 是 None，所以无法区分
        # "是否在重放里发过"：按 seq 猜会把 volatile（也是 None）一起吞掉。
        # 重复的那点流量交给前端按事件 id 去重。
        import time

        queues = [(entry.id, entry.live) for entry in entries]
        last_ping = time.time()
        while True:
            alive = False
            for _, q in queues:
                try:
                    env = q.get_nowait()
                except queue.Empty:
                    continue
                alive = True
                if isinstance(env, Envelope) and not _write(env.to_record()):
                    return
            if alive:
                # 忙时不睡（原注释的原意，曾经漏了这条分支 → 每轮只取一条还照睡
                # 退避间隔，实测 5 条突发按 2s 一条到达、第 5 条 8s 内没到）
                last_ping = time.time()
                continue
            now = time.time()
            if now - last_ping >= 15:
                if not _write({"type": "ping"}):
                    return
                last_ping = time.time()
            try:
                time.sleep(_LIVE_POLL_INTERVAL)  # 可中断式，不阻塞直播
            except KeyboardInterrupt:
                return


def run(host: str = "127.0.0.1", port: int = 18789, auth_token: str = "",
        workspace=None) -> int:
    """启动 serve（阻塞）：Tauri sidecar / 本地调试用。返回进程退出码。"""
    state = ServeState(workspace=workspace, auth_token=auth_token)
    state.start()
    server = ThreadingHTTPServer((host, port), _Handler)
    server.state = state  # type: ignore[attr-defined]
    server.daemon_threads = True
    # flush 必须显式：stdout 重定向到管道（Tauri sidecar 读 token= 行）时是块缓冲，
    # 不 flush 这两行会卡在缓冲区里，前端等不到 token。
    print(
        f"smith serve 监听 {host}:{port}（token: {'已配置' if state.token_provided else '已生成随机 token'}）",
        flush=True,
    )
    if not state.token_provided:
        print(f"token={state.auth_token}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.close()
        server.server_close()
    return 0

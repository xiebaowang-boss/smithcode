# 桌面端架构设计（Tauri + Python sidecar）

> 状态：设计稿（未实现）。目标是在不重写 Agent 核心的前提下，为 SmithCode
> 增加一个桌面宿主，与 REPL / TUI 并列。术语与既有设计保持一致，实现细节
> 以 [architecture.md](architecture.md) 为准。

## 1. 设计目标与不变量

**目标**：桌面端是**第三个宿主**，不是第二套 Agent。

1. **核心复用**：`agent.py` / `session.py` / `llm/` / `tools/` / `permission/` /
   `commands/` / `config.py` / `plan.py` / `goal.py` / `skills/` / `context/` /
   `process.py` / `cancel.py` 一行不改即可复用。
2. **不变量**：路径沙箱、保护路径、权限语义、非交互 fail-closed、输出截断、
   协作式取消（Esc / Ctrl+C 通道）全部保持现状。
3. **Python 3.9 兼容**：新代码遵守 [AGENTS.md](../AGENTS.md) 的兼容性红线。
4. **无网络端口**：后端以 Tauri **sidecar** 形式打包，前端只经 **stdio
   JSON-RPC** 通信；不监听 localhost、不需要 token / CORS / 重连。

## 2. 技术选型与进程模型

| 层 | 选择 | 说明 |
| ---- | ---- | ---- |
| 桌面壳 | **Tauri 2（Rust）** | 原生窗口 + 系统 WebView（Windows WebView2 / macOS WKWebView / Linux WebKitGTK），体积小 |
| 前端 | Web（HTML/CSS/JS，可选框架） | 富交互：流式 markdown、左右 diff、虚拟列表 |
| 后端 | **Python sidecar**（PyInstaller onefile） | 复用全部核心，作为子进程由 Tauri 启动 |
| 通信 | **stdio JSON-RPC 2.0（JSON Lines）** | 前端 `invoke` / `listen`，Rust 壳做透明转发 |

**为什么是 sidecar 而不是服务**：不占端口、无鉴权/CORS/Origin 问题、无断线重连，
进程生命周期天然与窗口绑定（关窗即杀）。安全面从「本机任意进程可连的 socket」
收缩到「只有父壳能读写的管道」。代价是要维护 Rust 壳与逐平台 sidecar 打包。

**进程模型**：`Tauri 壳进程` ── stdio ── `Python sidecar 进程` ── worker 线程。
Agent 依赖多处**进程级单例**（`plan` / `goal` / `skills` / `permission` 会话规则 /
`config` / `renderer`），因此**一个 sidecar = 一个会话**；多窗口用多 sidecar。

## 3. 组件与数据流

```
┌────────────────────────────────────────────────────────────┐
│ 前端（WebView，Tauri 加载）                                    │
│ ChatView / Composer / Sidebar / StatusBar / Modals           │
│   invoke('rpc', msg)  ──▶            ◀──  listen('rpc')       │
└──────────┬────────────────────────────────┬─────────────────┘
           ▼                                │
┌──────────────────────────────────────────┴─────────────────┐
│ Tauri 壳（Rust，仅传输，不理解语义）                            │
│   invoke→ sidecar.stdin.write(line)                          │
│   sidecar.stdout line → emit('rpc', line)                    │
└──────────┬────────────────────────────────▲─────────────────┘
           ▼ stdin（JSON-RPC 请求/通知）       │ stdout（JSON-RPC 通知/请求）
┌──────────────────────────────────────────┴─────────────────┐
│ Python sidecar：desktop/rpc.py（编解码 + 出站队列 + 写线程）    │
│   api.py（请求方法） / bridge.py（DesktopRenderer + DesktopView）│
└──────────┬─────────────────────────────────────────────────┘
           ▼
┌────────────────────────────────────────────────────────────┐
│ controller.py 宿主无关 headless 驱动（新增，三端共用）           │
│ Agent 核心（不动）+ renderer.py 接口                            │
└────────────────────────────────────────────────────────────┘
```

Rust 壳是**哑管道**：所有 RPC 语义（关联 id、服务端发起的请求）都由前端与
Python 两端处理，壳只做 stdin/stdout ↔ WebView 事件的双向搬运。

## 4. RPC 协议

### 4.1 帧格式

- **JSON Lines**：一行一条 JSON-RPC 2.0 消息，`\n` 分隔；JSON 会转义串内换行。
- **请求/响应**：`{"jsonrpc":"2.0","id":"<n>","method":"...","params":{...}}`
  → `{"jsonrpc":"2.0","id":"<n>","result":...}`（或 `{"error":{"code","message"}}`）。
- **通知**：无 `id`，用于事件推送。
- **双向对称**：前端与 sidecar 都能发起请求；`permission.request` /
  `ask.request` 是 sidecar 发起的请求，前端带同一 `id` 回结果。

### 4.2 前端 → sidecar（方法）

| 方法 | 参数 | 对应能力 |
| ---- | ---- | ---- |
| `session.submit` | `{text}` | 任务或斜杠命令，转 `HostController.submit` |
| `session.interrupt` | `{}` | 等价 Esc：`agent.interrupt()` |
| `session.resume` | `{target}` | `/sessions` 切换 / `-c` 恢复 |
| `session.snapshot` | `{}` | 晚连窗口：取当前历史 + 状态（§4.5） |
| `sessions.list` | `{limit}` | `sessions.list_sessions` |
| `models.list` | `{}` | `agent.models.list()` |
| `commands.list` | `{}` | `commands.all_commands()` + `help_text()`（补全/帮助） |
| `skills.list` / `skills.refresh` | `{}` | 技能列表与诊断 |
| `config.get` / `config.patch` | `{}` | 受限读写（**不返回凭据**） |
| `app.shutdown` | `{}` | 优雅退出（关闭转录、结束进程） |

### 4.3 sidecar → 前端（通知，`Renderer` / `HostView` 映射）

| 通知 | 字段 | 来源 |
| ---- | ---- | ---- |
| `stream` | `kind: reasoning\|content, delta` | `Renderer.stream` |
| `stream_done` | — | `Renderer.stream_done` |
| `tool_call` | `tool_id, line, display, name` | `Renderer.tool_call` |
| `tool_preview` | `tool_id, detail` | `Renderer.tool_preview` |
| `tool_result` | `tool_id, result, expand, is_error` | `Renderer.tool_result` |
| `notice` | `level, text` | `Renderer.info/warn/error` |
| `plan` | `summary, rendered, created, tool_id` | `Renderer.plan` |
| `title` | `title` | `Renderer.title_changed` |
| `turn_start` / `turn_end` | `status, elapsed, model, effort` | `HostView` |
| `session_reset` / `session_resume` | `history, meta` | `HostView` |
| `selection` | `title, command, items, size` | `HostView.show_selection` |
| `status` | 用量 / goal / plan / busy 快照 | `HostView.status` |
| `exit` | — | `HostView.exit` |

- **合批**：`stream` delta 在发送侧按 ~30ms 合并成一条通知，避免逐 token 刷帧。
- **`session_resume` 复用**：`/sessions` 切换、`-c` 恢复、窗口重载快照都走它。

### 4.4 sidecar → 前端（服务端发起的请求）

| 请求 | 参数 | 前端回执 |
| ---- | ---- | ---- |
| `permission.request` | `{prompt, valid, hint, detail, descriptions, content}` | `{value: "y"\|"n"\|"a"}` |
| `ask.request` | `{questions:[...]}` | `{values:[...]}` |

对应 `Renderer.confirm_choice` / `Renderer.ask_form` 的阻塞语义（见 §5）。前端
关闭窗口 / 超时 / 管道断开时，Python 侧一律按**拒绝（fail-closed）**返回，与
非交互 fail-closed 一致。

### 4.5 晚连与窗口重载

无需事件重放：sidecar 保留当前会话的**快照**（历史消息 + `status` + `plan` +
`title`）。前端启动或 `session.snapshot` 时取一次全量再进入增量流。运行态归
sidecar 所有，刷新页面不打断正在执行的任务。

### 4.6 stdout 纯净性（重要约束）

sidecar 的 **stdout 是协议专用**，任何杂散 `print()` 都会破坏帧同步：

- 启动时把真实 stdout 复制到一个专用句柄供 `rpc.py` 写出，并将
  `sys.stdout` 重定向到 `sys.stderr`；所有日志走 stderr。
- `DesktopRenderer` 必须在任何输出发生**之前**通过 `renderer.set_renderer()`
  替换默认的 `ConsoleRenderer`（与 TUI 启动时同一机制）。欢迎横幅、工具摘要、
  错误提示都必须经渲染后端，不能直接 `print`。
- 子进程退出码与 stderr 由 Rust 壳收集，用于崩溃诊断。

## 5. 线程模型

| 线程 / 循环 | 职责 |
| ---- | ---- |
| stdin reader（主线程） | 逐行读请求/回执，分发到 `HostController` 或唤醒等待中的确认 |
| writer 线程 | 从线程安全出站队列取消息，串行写协议 stdout（加锁） |
| Agent worker（每轮一个线程） | 同步执行 `run_with_goal`（阻塞），调用 `Renderer` / `HostView` |
| UI（WebView 内部） | 独立渲染线程，经 Tauri IPC 与 Rust 壳通信 |

- `Renderer` / `HostView` 方法在 **worker 线程**被调用：只做「封装成 RPC 通知 →
  入队」，**绝不直接写 stdout**（避免与 writer 线程竞争）。
- **服务端发起的请求**（permission / ask）：worker 线程创建 `threading.Event`
  并 `wait()`；stdin reader 收到带 id 的回执后填充结果并 `set()`。与
  `tui/bridge.py` 的模式一一对应，只把 Textual 消息换成 stdio 帧。
- 同一时刻只允许一轮任务（busy 守卫，与 TUI 一致）；`submit` 在运行中按现有
  规则拒绝 `/new` / `/sessions`。
- `Agent.interrupt()` 线程安全（`cancel.py`），stdin reader 可直接调用。

## 6. Python sidecar 模块

### 6.1 `controller.py`（落地前置项，三端共用）

目前 `cli.py:106-142` 与 `tui/app.py:683-796` 各实现了一遍「收输入 →
`commands.dispatch` → 处理 `CommandResult`（`session_reset` / `start_task` /
`echo_input` / `select` / `refresh_status`）→ 开后台任务 → busy 守卫」。桌面端
会变成第三份，必须先抽成宿主无关的控制器，否则行为必然漂移。

```python
class HostView:                     # 宿主实现（TUI / desktop / REPL 适配）
    def turn_start(self) -> None: ...
    def turn_end(self, status: str, elapsed: float) -> None: ...
    def session_reset(self) -> None: ...
    def session_resume(self, history, meta) -> None: ...
    def status(self, snapshot) -> None: ...
    def show_selection(self, select) -> None: ...
    def exit(self) -> None: ...

class HostController:
    def __init__(self, agent, view, renderer): ...
    def submit(self, text: str) -> None: ...   # 斜杠命令或任务，内部区分
    def interrupt(self) -> None: ...           # 幂等，转发 agent.interrupt()
    def resume(self, target) -> None: ...       # 就地恢复，触发 session_resume
    @property
    def busy(self) -> bool: ...
```

渲染（流式 / 工具 / 通知 / 计划）仍走 `renderer.Renderer`；生命周期与宿主动作
走 `HostView`。重构是机械的：`cli.py` / `tui/app.py` 改为薄适配器，测试保持绿。

### 6.2 目录

```
src/smithcode/
  controller.py          # 新增：宿主无关 headless 驱动
  desktop/
    __init__.py          # main()：解析 --workspace/--add/--resume 等，装配并启动
    __main__.py          # python -m smithcode.desktop
    rpc.py               # JSON-RPC 编解码、出站队列、writer 线程、请求关联（纯逻辑，可测）
    bridge.py            # DesktopRenderer(Renderer) + DesktopView(HostView) → RPC 通知
    api.py               # 前端方法注册（session/models/sessions/commands/config/skills）
src-tauri/               # Tauri Rust 壳
  Cargo.toml
  tauri.conf.json        # externalBin、窗口、bundle 配置
  src/main.rs            # 启动 sidecar、stdio ↔ emit 双向转发、退出时 kill
frontend/                # Web UI（构建产物由 Tauri 打包为 frontendDist）
  src/...
```

前端目录可独立成前端工程（有构建步骤）或保持无构建原生 JS；`src-tauri`
的 `frontendDist` 指向其产物目录即可。

## 7. Rust 壳职责

1. **启动 sidecar**：`tauri-plugin-shell` 以 `new_sidecar("smithcode-backend")`
   拉起子进程，传入工作区/恢复参数。
2. **前端 → sidecar**：`#[tauri::command] fn rpc(msg: String, state)` → 子进程
   stdin 写一行（加换行）。
3. **sidecar → 前端**：读取 `CommandEvent::Stdout(line)` → `app.emit("rpc", line)`。
   stderr 收集用于诊断日志。
4. **生命周期**：监听窗口关闭事件，发送 `app.shutdown` 后 kill 子进程；子进程
   意外退出时通知前端显示错误并禁用输入。
5. **不解析协议**：壳不认识任何 method，纯粹透传，便于协议演进与测试。

## 8. 前端设计

- **状态**：单一 reducer，输入为 RPC 通知流；消息模型镜像 `tui/chat.py` 的语义
  item（`User` / `Assistant` / `Notice` / `Block` / `Tool*` / `Plan` …），协议只
  传语义，缩进/着色由前端样式决定。
- **视图**：ChatView（虚拟列表、流式气泡、可折叠工具块、左右对照 diff）、
  Composer（多行输入、`/` 菜单、历史、Ctrl+Enter 换行、Esc 中断）、Sidebar
  （会话标题、用量/上下文条、目标卡片、计划清单）、StatusBar（模型 · 思考强度 ·
  token · busy · 停止提示）、Modals（权限 / 提问 / 选择）。
- **RPC 客户端**：薄封装 `invoke('rpc', …)` / `listen('rpc', …)`，负责 id 关联与
  处理 sidecar 发来的 `permission.request` / `ask.request`。
- **复用**：markdown 渲染、diff 对照、token 缩写等纯函数可从 `tui/render.py`
  （`side_by_side_diff`）移植为 TS；或协议传结构化 diff 数据。

## 9. 生命周期与打包

- **sidecar 构建**：PyInstaller `--onefile` 生成自包含可执行文件，产物按 Tauri
  约定命名 `binaries/smithcode-backend-<target-triple>[.exe]`，在
  `tauri.conf.json` 的 `bundle.externalBin` 声明。
- **逐平台**：Windows x64 / macOS（arm64+x64，需签名与公证）/ Linux。sidecar
  与壳必须同架构。
- **配置与数据**：沿用 `~/.smithcode`（config / credentials / sessions），桌面端
  不新建存储口径。
- **首启**：无配置时复用 `wizard.py` 的引导（经渲染后端输出，或提供原生配置页）。

## 10. 安全边界

- 无监听端口，攻击面仅限父子进程管道；不引入 token / CORS / Origin 问题。
- REST 类能力（§4.2）**不提供任意文件读写**：文件操作只能经 Agent 工具链，
  复用路径沙箱与权限引擎；`credentials.json` 内容永不返回前端。
- 权限语义、保护路径、越界确认、fail-closed 全部沿用 `permission/`，桌面只是
  换了一个 `confirm_choice` 的实现。
- 工作区由启动参数/用户选择决定；sidecar 越权访问仍需经现有确认流程。

## 11. 实施阶段

| 阶段 | 内容 | 验收 |
| ---- | ---- | ---- |
| P0 | 抽 `controller.py`，`cli.py` / `tui/app.py` 改用 | `pytest` 全绿，行为不变 |
| P1 | `desktop/rpc.py` + `bridge.py` + `api.py`；用测试客户端直驱 sidecar | 无 GUI 即跑通一轮流式任务与权限回执 |
| P2 | Tauri 壳（stdio 转发）+ 前端 MVP（Composer / 流式 / 工具块 / 权限弹窗） | 核心对话与确认闭环 |
| P3 | diff 对照、计划/目标侧栏、会话切换与快照、命令菜单、上下文条 | 对齐 TUI 功能 |
| P4 | PyInstaller sidecar + `tauri build`、签名/公证、崩溃兜底 | 三平台冒烟 |

## 12. 测试策略

- **纯逻辑**：`tests/test_controller.py`——用假 Agent / 假 view 驱动，断言
  `CommandResult` 分支（`session_reset` / `start_task` / busy 拒绝）。
- **协议**：`tests/test_desktop_rpc.py`——JSONL 帧编解码、id 关联、出站合批、
  断开时确认 fail-closed。sidecar 逻辑与 Rust/WebView 解耦，可在纯 Python 下测。
- **集成**：进程内用假 LLM 装置，把「请求行」喂给 sidecar 的 stdin 读取函数，
  断言产出通知序列，不依赖真实 API、不依赖 GUI。
- Rust 壳只做透传，测试重点在「收到 stdout 行能 emit」「退出能 kill」。

## 13. 风险与开放问题

- **stdout 纯净性**：任何遗漏的 `print` 都会破坏协议——需在启动早期重定向
  `sys.stdout` 并统一经渲染后端输出，加测试守护（§4.6）。
- **打包复杂度**：需要 Rust 工具链与逐平台 PyInstaller 产物；macOS 公证、
  Windows 签名是额外运维成本。
- **多会话**：进程级单例限制「一 sidecar 一会话」；多窗口需多进程，需权衡资源。
- **前端工程量**：虚拟列表 + markdown + diff 是主要工作量，建议独立前端工程并
  将构建产物内嵌，或保持无构建原生 JS 以简化打包。
- **备选**：若日后需要远程/无头 attach 或多前端复用，可在 sidecar 之上再加一层
  薄 HTTP+WS；`Renderer` / `HostView` 接口不变，仅新增一种传输实现。

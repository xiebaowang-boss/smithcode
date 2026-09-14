# MCP 子系统架构与实现

本文是 `src/smithcode/mcp/` 的完整设计说明：分层与职责、数据模型、配置与密钥、OAuth、
客户端运行时、生命周期与竞态处理、工具注册、与 Agent 的交互、命令与向导、线程模型、
诊断与测试。`docs/architecture.md` 的「MCP」节是摘要版，本文是展开版。

## 1. 定位与范围

SmithCode 通过 **Model Context Protocol** 接入外部工具服务器，把外部工具注册进内置工具
注册表，与内置工具共用权限、调度、展示与截断机制。

- **传输**：stdio（本地子进程）、Streamable HTTP（远程，推荐）、SSE（远程，legacy）。
- **协议**：基于官方 `mcp` Python SDK v2（要求 Python ≥ 3.10）；`Client` 的 `mode="auto"`
  会先探测现代协议（2026-07-28），失败回退到 initialize 握手，因此新旧 server 都能接。
- **鉴权**：`${VAR}` 引用链（环境变量 / 本地凭据库）、请求头静态 token、OAuth 2.1
  （发现 / DCR / PKCE / 刷新）。
- **不在范围内**（SDK 已具备，留作扩展）：resources / prompts 作为一等能力、
  sampling / elicitation 的人工回路、Client ID Metadata Documents、mTLS / 代理矩阵。

设计原则与项目整体一致：

1. **线程模型不引入 asyncio 到主流程**——asyncio 只存在于 `runtime.py` 的专用线程，
   Agent、工具、渲染、权限层保持同步；
2. **失败隔离**——单个 server 连接失败不影响 Agent 启动与其他 server；
3. **非交互 fail-closed**——管道 / CI 下需要用户确认或授权的路径一律拒绝而非挂起；
4. **不可信内容**——外部工具的描述、annotations、返回内容按不可信处理；
5. **密钥不回显**——配置只写引用，值单独存放，全链路经 Redactor。

## 2. 目录与分层

| 文件 | 职责 |
| --- | --- |
| `config.py` | 双作用域配置：加载 / 合并 / 校验 / 写入（用户 TOML + 项目 JSON），传输字段归一 |
| `secrets.py` | `${VAR}` / `${VAR:-default}` 引用展开、凭据库读写、全局 Redactor |
| `auth.py` | OAuth token / client_info 持久化、浏览器回调服务、交互/非交互策略 |
| `runtime.py` | `AsyncRuntime`：唯一的 asyncio 事件循环线程，对外提供同步调度原语 |
| `connection.py` | `SdkConnection`：SDK `Client` 的同步门面、传输构造、取消桥接、断开检测、进度、订阅 |
| `factory.py` | 按 `cfg.type` 构造连接的唯一切口（当前统一返回 `SdkConnection`） |
| `catalog.py` | 工具命名、inputSchema 规整、CallToolResult → 文本映射（脱敏 + 截断） |
| `service.py` | `McpService`：会话级连接管理、状态机、动态注册/反注册、调用路由、配置变更 |
| `wizard.py` | 添加向导的纯状态机（TUI / REPL 共用）+ REPL 行式渲染器 + `apply_plan` |
| `templates.py` | 内置模板（stdio 命令模板 + 远程 URL/OAuth 模板） |
| `errors.py` | `McpError` / `McpConfigError` / `McpAuthError` 异常层级 |

外部对接点：

| 位置 | 关系 |
| --- | --- |
| `agent.py` | 创建 `McpService`，在 `start()` / `close()` 挂钩生命周期 |
| `tools/base.py` | `register_dynamic` / `unregister_dynamic` / `DYNAMIC`，动态工具进入同一注册表 |
| `permission/` | 动态工具无显式规则，默认 `ask`；`serial=True` 进顺序屏障 |
| `renderer.py` | 连接状态、进度、日志、告警统一经渲染后端输出 |
| `commands/mcp.py` | `/mcp` 命令族（状态、添加、授权、生命周期） |
| `tui/` | MCP 向导面板、服务器操作菜单、busy 守卫 |
| `llm/prompts.py` | 「MCP 工具」行为节：命名来源、不可信处理、未连接时不臆造 |

依赖方向（自顶向下）：

```
命令层 / 向导 / Agent
        │  同步 API
        ▼
   service（会话级聚合根）
        │  create_connection(cfg, resolved, runtime, callbacks, interactive)
        ▼
   factory ──► connection（同步门面） ──► auth（OAuth provider / token 存储）
        │  run_coroutine_threadsafe / future.result
        ▼
   runtime（专用 asyncio 事件循环线程）
        │  async
        ▼
   SDK Client ──► transport：stdio_client / streamable_http_client / sse_client
        │
        └── catalog（命名 / schema / 结果）   secrets（引用 / 凭据 / 脱敏）
```

## 3. 数据模型

### 3.1 `ServerConfig`（归一化配置，`config.py`）

| 字段 | 含义 |
| --- | --- |
| `name` | 服务器名（工具前缀、凭据键、状态键） |
| `type` | `stdio` / `http` / `sse`（加载时把 `local`→stdio、`remote`/`streamable-http`→http） |
| `command` | stdio 的 argv 数组 |
| `env` | stdio 子进程环境变量（值可含 `${VAR}`） |
| `url` | http / sse 端点 |
| `headers` | http / sse 请求头（值可含 `${VAR}`） |
| `oauth` | http / sse 是否走 OAuth |
| `cwd` / `timeout` / `enabled` | 工作目录 / 单次调用超时（默认 60s）/ 启停 |
| `scope` / `source` | 来源作用域（user/project）与来源文件（展示、诊断、写回定位） |

两个派生属性：

- `fingerprint`：`type/command/url/headers(键集)/oauth/cwd/env(键集)` 的 sha256 前 16 位。
  `reload()` 用它判断配置是否需要重连。
- `target`：终端展示目标——stdio 显示命令串，远程显示 URL。

### 3.2 `ResolvedServer`（展开后的 spawn / 请求参数，`secrets.py`）

`command` / `env` / `headers` / `cwd` 全部完成 `${VAR}` 展开；`missing` 收集缺失且无默认值的
变量名（去重保序）。`resolve()` 不抛异常——缺失由调用方决定是补录还是 fail-closed。

### 3.3 `ToolSpec`（已暴露的工具，`catalog.py`）

`server` / `original`（服务端原名）/ `exposed`（本地唯一暴露名）/ `description` /
`input_schema` / `annotations` / `enabled`。`read_only` 读 annotations 的 `readOnlyHint`
（不可信，仅展示参考）；`to_schema()` 生成发给 LLM 的 function-calling schema。

### 3.4 `_Entry` 与 `ServerStatus`（运行时，`service.py`）

`_Entry` 是每个服务器的可变运行时状态：`cfg`、`conn`、`state`、`error`、`missing`、
`tools`、`generation`。`ServerStatus` 是给命令层/UI 的不可变快照（含 `state_label` /
`scope_label` 中文标签）。

## 4. 配置子系统（`config.py`）

### 4.1 双作用域与合并

| 作用域 | 文件 | 结构 |
| --- | --- | --- |
| 用户级 | `~/.smithcode/config.toml` | `[mcp.servers.<名称>]`（tomlkit 写入保注释） |
| 项目级 | `<工作区>/.smithcode/mcp.json` | `{"mcpServers": {...}}`（兼容 Claude / Cursor / VS Code 片段） |

- 同名服务器**项目条目整体覆盖**用户条目（字段不合并），避免"半个配置"。
- 启停状态 `enabled` 是条目自己的字段，写在定义它的文件里（默认启用时省略该键）。
- 加载全程容错：坏条目（缺 command/url、不支持的 type、非表结构……）只记入
  `diagnostics` 并跳过，一个坏 server 不影响其余条目与启动。

### 4.2 传输字段与生态兼容

`type` 别名归一后只保留 `stdio` / `http` / `sse`。兼容写法：

```jsonc
{"type": "stdio", "command": "npx", "args": [...]}                 // 字符串 + args
{"command": ["npx", "-y", "pkg"]}                                  // 数组形式
{"type": "http",  "url": "https://.../mcp", "headers": {...}}      // VS Code / Claude
{"type": "streamable-http", "url": "..."}                          // 别名 → http
{"type": "remote", "url": "...", "oauth": {}}                      // opencode 风格
{"type": "sse", "url": "..."}                                      // legacy
```

`oauth` 接受 `true` 或对象（对象形式当前等价于 true，为 scopes/client_id 预留）；stdio
上配 `oauth` 会记录诊断并忽略。

### 4.3 写入

- 用户级用 tomlkit：`env` / `headers` 渲染为**内联表**，使同一服务器的属性聚合在同一段；
- 项目级写 JSON，保留其他 keys 与未知字段；`enabled` / `oauth` 放条目最前；
- 所有写入走 `_atomic_write`（临时文件 + `os.replace`，失败不破坏原文件）。

## 5. 密钥子系统（`secrets.py`）

### 5.1 解析链

```
${VAR} / ${VAR:-default}
   ├─ 进程环境变量 os.environ
   ├─ ~/.smithcode/credentials.json 的 mcp.<服务器>.<变量>
   └─ 引用自带默认值（:-default）
   └─ 都没有 → 记入 missing（不带着空值拉起 server）
```

`resolve(cfg)` 展开 `command` / `env` / `headers` / `cwd` 四处引用，返回 `ResolvedServer`。

### 5.2 凭据库与脱敏

- `store_secret(server, var, value)` 写 `credentials.json`（原子写；POSIX 下 mkstemp 即
  0600），保留文件内其他内容；
- 所有展开出的值登记进程级 `Redactor`（线程安全）；`Redactor.add` 忽略 < 4 字符的值
  （全局替换会误伤正常文本）；
- `scrub()` 按值长度降序替换为 `***`，应用于工具结果、stderr / 日志、向导预览。

## 6. OAuth 子系统（`auth.py`）

SDK 的 `OAuthClientProvider` 负责协议全流程（Protected Resource Metadata 发现 →
授权服务器元数据 → 动态客户端注册 → PKCE 授权码 → 换 token → 刷新）。本模块补齐三块
宿主能力：

### 6.1 Token 持久化

`FileTokenStorage` 实现 SDK 的 `TokenStorage` 协议（四个异步方法）：

| 数据 | 位置 |
| --- | --- |
| `tokens`（access / refresh / expires_in / scope） | `~/.smithcode/mcp_auth.json` → `servers.<名称>.tokens` |
| `client_info`（DCR 注册结果，含 client_id） | 同文件 → `servers.<名称>.client_info` |

- 独立于 `config.toml`；原子写、POSIX 0600；`anyio.to_thread` 做文件 IO（把阻塞操作
  移出事件循环）；
- 读写 token 时调用 `redactor().add(...)`，access / refresh token 不进终端与转录；
- `has_tokens(server)` 供启动预检（同步只读）；`clear_tokens(server)` 登出/重授权。

### 6.2 浏览器回调

`OAuthSession` 为一次连接构造 provider 与本地回调资源：

- **固定回调端口** `127.0.0.1:3334`（被占用时退回随机端口并告警），保证 `redirect_uri`
  跨运行稳定、DCR 登记的地址不失效；
- 交互模式下 `_CallbackServer` 提前起一个临时 HTTP 服务，`GET /callback` 解析
  `code` / `state` / `iss`（或 `error`）投入队列并回一个"授权完成"页面；
- `redirect_handler` 打印授权链接并 `webbrowser.open`；`callback_handler` 在
  `anyio.to_thread` 里等队列（超时 180s），不阻塞事件循环；
- 连接关闭时回收回调服务。

### 6.3 交互 / 非交互策略（关键安全边界）

| 场景 | 行为 |
| --- | --- |
| 后台连接（启动 / enable / reconnect） | 无 token 时**不连接**，置 `needs_auth`；`redirect_handler` 若被触发一律抛 `McpAuthError` |
| 用户显式 `/mcp auth <名称>` | 以 `interactive=True` 重连，允许开浏览器完成授权 |
| CI / 管道 | 同"后台连接"：fail-closed，提示用 `/mcp auth` 或改用静态 token |

授权失败（拒绝 / 超时 / token 交换失败）统一归入 `needs_auth`，错误文案面向前用户且
给出可操作步骤。

## 7. 客户端运行时（`runtime.py` + `connection.py`）

### 7.1 `AsyncRuntime`：唯一的事件循环线程

```
主线程 / 线程池                    smithcode-mcp-loop 线程
   runtime.run(coro) ──submit──►  asyncio.run_coroutine_threadsafe
   future.result(timeout) ◄──────  coro 执行，结果回写 future
```

- `start()` 幂等：起守护线程 → `new_event_loop` → `run_forever`；
- `submit(coro)` 返回 `concurrent.futures.Future`；`run(coro, timeout)` 同步等待；
- `stop()` 停循环并 `join`，收尾时取消遗留任务；
- **约定：loop 线程内禁止阻塞调用**（文件 IO 用 `anyio.to_thread`，浏览器等待用线程）。

### 7.2 `SdkConnection`：同步门面

对外接口与服务层的期望一一对应：

| 方法 | 说明 |
| --- | --- |
| `start() -> dict` | 启动传输 + SDK 握手，返回 `server_info`；失败抛 `McpError` / `McpAuthError` |
| `list_tools() -> list[dict]` | 分页拉取（上限 100 页防死循环），返回旧客户端同形 dict |
| `call_tool(name, args, timeout) -> dict` | 调用工具，返回 `CallToolResult` 的 by-alias dump |
| `close()` | 幂等关闭：取消订阅任务 → 关闭 SDK 上下文 → 关 stderr 文件 / 回调服务 |
| `alive` | `_connected and not _closed and not _eof` |
| `stderr_tail(n)` / `malformed_lines` | 诊断 |

握手预算与超时：握手 `max(20, min(cfg.timeout, 60))` 秒；`list_tools` 至少 10s；关闭 10s。

**结果归一**：`model_dump(by_alias=True, exclude_none=True)` 让 SDK 的类型化对象
（`Tool` / `CallToolResult`）回到与旧自研客户端一致的 camelCase dict，
`catalog` 与注册层因此零改动。

### 7.3 三种传输的构造

| type | 构造 |
| --- | --- |
| `stdio` | `StdioServerParameters(command, args, env={**os.environ, **resolved.env}, cwd, encoding_error_handler="replace")` → `stdio_client(params, errlog=临时文件)` |
| `http` | `httpx2.AsyncClient(headers=resolved.headers, auth=OAuthProvider \| None, timeout=Timeout(30, read=max(300, cfg.timeout)))` → `streamable_http_client(url, http_client=...)`（客户端由连接自己关闭） |
| `sse` | `sse_client(url, headers=resolved.headers, timeout=30, sse_read_timeout=max(300, cfg.timeout))` |

说明：

- stdio 保持旧行为——父进程环境 + 服务器 env 合并后传入（SDK 默认只继承白名单）；
- stderr 落临时文件（SDK 的 `errlog` 要求真实 fd），供 `/mcp logs`；HTTP 传输的
  诊断来自内部备注（传输异常 / 解析错误）；
- OAuth provider 只在 `cfg.oauth` 时构造，挂在同一个 `httpx2.AsyncClient` 上。

### 7.4 取消桥接

工具调用可能运行在 Agent 的 run 线程（serial 工具），项目用协作式取消令牌
（`cancel.py`）。门面在等待 future 时以 100ms 轮询：

```
future.result(0.1)
   ├─ 正常返回 → 结果
   ├─ 超时（未到 deadline）→ 检查 current_token() / _eof，继续等
   ├─ token.cancelled → future.cancel()，抛 McpError("用户中断")
   └─ 超过 deadline → future.cancel()，抛 McpError("…超时")
```

`future.cancel()` 会取消远端协程（CancelledError 沿 anyio / httpx2 栈冒泡），不需要
`notifications/cancelled` 的手工构造。

### 7.5 断开检测（SDK 的缺口补齐）

SDK v2 不推送"连接断开"事件（干净 EOF 被静默）。因此在传输外层包一层
`_monitored_transport`：一条泵任务代读原读流，转发到代理流；原流读到 EOF 或抛错即
回调 `on_eof`。`SdkConnection._handle_eof()` 在非主动关闭时回调服务层的
`on_closed(conn)`，等价于旧自研客户端读线程的崩溃检测。

### 7.6 进度与列表订阅

- **进度**：`call_tool(progress_callback=...)`，回调 `(progress, total, message)`；
  按 10% 里程碑经 renderer 展示，`total` 未知时不展示，避免刷屏；
- **列表变更**：现代协议（2026-07-28+）用 `Client.listen(tools_list_changed=True)` 订阅流，
  收到事件即触发服务层刷新；旧协议抛 `ListenNotSupportedError` 后静默退出，由
  `message_handler` 接收 `notifications/tools/list_changed` 承担。两条路径都不会
  让订阅不可用影响连接本身。

## 8. 服务层（`service.py`）

`McpService` 是会话级聚合根，由 `Agent` 持有（进程内单例）。

### 8.1 状态机

| 状态 | 标签 | 触发 |
| --- | --- | --- |
| `pending` | 等待连接 | 刚创建 / enable / reconnect / authorize |
| `connecting` | 连接中 | `_connect` 开始 |
| `connected` | 已连接 | 握手 + 列工具 + 注册成功 |
| `failed` | 连接失败 | 握手 / 列工具失败，或运行中断开 |
| `missing_env` | 缺少密钥 | `${VAR}` 展开缺变量（预检） |
| `needs_auth` | 需要授权 | OAuth 无 token 或授权失败 |
| `disabled` | 已停用 | 配置 `enabled=false` / 用户停用 |
| `disconnected` | 已断开 | 手动断开 / 重连中 |

### 8.2 启动流程

```
start()（Agent.start 调用）
  ├─ runtime.start()                 # 启动 loop 线程
  ├─ load_servers()                  # 双作用域配置 + diagnostics
  ├─ 项目配置警示（会拉起本机进程）
  └─ 逐个服务器：
       disabled        → DISABLED
       missing secrets → MISSING_ENV（不连接）
       oauth 无 token   → NEEDS_AUTH（不连接）
       否则             → 线程池 submit(_connect)
```

连接在专用线程池（4 workers）后台执行，失败隔离、不阻塞启动。

### 8.3 连接与竞态防护（关键）

`_connect` 运行在线程池线程，握手可能持续数秒；期间配置可能被 disable / remove /
reconnect。防护机制是**连接代际 + 连接身份**：

```
_connect(name, interactive):
  ① 锁内 entry.generation += 1，记 generation
  ② 预检 missing / oauth token
  ③ create_connection(...) → start() → list_tools()
  ④ 回写前校验（锁内）：
       self._entries.get(name) is entry  且  entry.generation == generation
       且 not _stopped 且 state != DISABLED
     ├─ 成立：entry.conn = conn → 注册工具 → CONNECTED
     └─ 不成立：conn.close()，丢弃这次结果
  _disconnect() 与 _on_closed() 都会 generation += 1，使在途连接失效
```

`_on_closed(conn)` 按**连接身份**判定（`entry.conn is conn`），因此旧连接的迟到回调
不会误伤新连接。这解决了三类历史竞态：

1. 重连 / 断开后再次崩溃被静默吞掉（状态卡在"已连接"、工具不反注册）；
2. 握手期间 disable 被结果覆盖（服务器仍被拉起并注册工具）；
3. 握手期间 remove 遗留孤儿进程与已注册工具。

`_connect` 还兜底捕获非预期异常（不只 `McpError`），保证状态不会停在 `connecting`。

### 8.4 动态工具注册

连接成功后 `_sync_tools(entry, tool_defs)`：

```
_unregister(entry)                       # 先反注册旧名
taken = set(DYNAMIC)                     # 全局已用暴露名
for tool_def in tool_defs:
    spec = catalog.build_spec(server, tool_def, taken)
    register_dynamic(spec.to_schema(), func, serial=True,
                     describe=lambda args: f"mcp {server}.{tool}", display="inline")
entry.tools = specs
```

- 执行闭包 `func(**args) -> str` 调 `service.call(server, tool, args)`；
- `serial=True`：外部服务器状态未知，批量执行时作为顺序屏障；
- 断开 / 工具列表变化 / 停用 / 删除时 `_unregister` 反注册，模型侧无感知。

### 8.5 调用路由

```
模型 tool_call(mcp__server__tool)
   → tools/base FUNCTIONS[name]（动态闭包）
   → service.call(server, tool, args)
        ├─ 取 entry.conn；无连接 / 不 alive → 返回"错误: MCP 服务器 X 未连接"
        ├─ conn.call_tool(tool, args, timeout=cfg.timeout)
        └─ catalog.format_result(result, redactor())
   → 字符串结果回传模型
```

任何失败都翻译成给模型的错误文本，不抛异常给模型看。

### 8.6 配置变更操作

| 方法 | 行为 |
| --- | --- |
| `add(cfg, scope)` | 写配置（用户/项目）→ 停旧连接 → 建新 entry → 后台连接；跨作用域同名冲突明确报错 |
| `remove(name)` | 断开 → 反注册 → 按条目来源删配置 |
| `set_enabled(name, enabled)` | 写 `enabled` 到定义文件；启用则重新连接，停用则断开 |
| `reconnect(name)` | 断开 → PENDING → 后台重连 |
| `authorize(name)` | 断开 → PENDING → 以 `interactive=True` 后台重连（允许浏览器授权） |
| `reload()` | 重新装载配置并 diff（新增→连接、删除→移除、指纹变化→重连）；当前无命令入口（预留） |

## 9. 工具目录（`catalog.py`）

- **命名**：`mcp__<服务器>__<工具>`（对齐 Claude Code），非 `[A-Za-z0-9_-]` 字符替换为
  `_`，总长截断 64，冲突补 `_2` / `_3` 后缀；权限规则可用通配符命中（`mcp__github__*`）。
- **schema**：去掉 `$schema`、强制 `type=object`、补空 `properties`；描述加
  `[MCP:<服务器>]` 前缀并截断 1024 字符。
- **结果映射**：`content` 数组按类型处理——`text` 原文、`image` / `audio` 降级为
  占位提示（终端不支持展示）、`resource` 取文本否则 `[资源 uri]`、`resource_link`
  转 `[链接] uri`、其余 JSON 序列化；`structuredContent` 追加 JSON；`isError` 加
  `错误: ` 前缀；最后统一脱敏 + `truncate_output(MAX_TOOL_OUTPUT)`。

## 10. 与 Agent 的交互

### 10.1 生命周期挂钩

```python
class Agent:
    def __init__(...):
        self.mcp = McpService()

    def start(self):
        self.models.bootstrap()
        self.refresh_skills()
        instructions.refresh()
        self.mcp.start()          # 后台连接，失败隔离、不阻塞

    def close(self):
        self.mcp.stop()           # 关连接 → 停 loop 线程 → 关线程池
        ...
```

### 10.2 工具调用主链路

```
Agent.run(user_input)
  └─ _BatchScheduler.run(tool_calls)          # 边预检边执行
       ├─ _preflight(tc)                       # 主线程：解析参数、describe、路径预检、权限
       │    ├─ serialize? SERIAL[name]         # MCP 工具 serial=True → 顺序屏障
       │    └─ permission.check(name, args)    # 无显式规则 → 默认 ask
       ├─ serial 计划：_flush() 并行波次 → 主线程执行
       └─ 并行计划：线程池波次 → 按提交序收集
            └─ _collect → 追加 role=tool 消息 → 回传模型
```

要点：

- **权限**：MCP 工具在 `DEFAULT_RULES` 中没有条目，按"无匹配默认 ask"逐个确认；
  用户可用通配符规则收紧（`deny`）或放宽（`allow`）；非交互下 ask 直接拒绝。
- **调度**：`serial=True` 使 MCP 调用成为顺序屏障（外部服务器状态未知），并且
  `describe` 生成 `mcp <server>.<tool>` 的终端摘要。
- **截断**：结果经 `agent._finish` → `truncate_output(MAX_TOOL_OUTPUT)` 再入历史。
- **系统提示词**（`llm/prompts.py`「MCP 工具」节）：说明命名来源、把描述与输出当
  不可信内容、未连接（缺密钥 / 需授权 / 启动失败）时不臆造调用、批量场景不要堆叠。

### 10.3 一次工具调用的端到端时序

```
时序参与者：模型 → Agent(run 线程) → 权限 / 渲染 → McpService · SdkConnection → Runtime(loop 线程) → MCP Server

模型            工具调用 mcp__srv__tool(args)
 │
 ▼
Agent / _BatchScheduler
 ├─ ① _preflight：json.loads(args)、describe="mcp srv.tool"、渲染工具行
 ├─ ② permission.check(name, args)
 │      └─ 无匹配规则 → 默认 ask ─► 权限 / 渲染：确认框（非交互 → 直接 deny）
 │             └─ 允许 y/a → 继续；拒绝 → 任务终止，剩余 tool_call 补占位
 ├─ ③ serial=True 顺序屏障：先冲刷前面的并行波次，再在主线程执行
 └─ ④ run() = FUNCTIONS["mcp__srv__tool"](**args)     ← 查注册表命中闭包
 │
 ▼
McpService.call("srv", "tool", args)     （闭包把暴露名还原成原始 server / tool）
 ├─ 取 entry.conn；None 或不 alive → 返回 "错误: MCP 服务器 srv 未连接"
 └─ ⑤ conn.call_tool("tool", args, timeout=cfg.timeout)
 │
 ▼
SdkConnection.call_tool（同步门面）
 ├─ ⑥ runtime.submit(_acall(...))        ← 协程投递到 loop 线程，立即返回 future
 └─ 等待期：future.result(0.1) 轮询 token 取消 / deadline 超时 / _eof 断开
 │
 ▼
AsyncRuntime / loop 线程
 └─ ⑦ await Client.call_tool(name, args, progress_callback=...)
 │
 ▼
SDK Client → 传输(stdio / streamable-http / sse) → MCP Server
 ├─ ⑧ 发送 tools/call{ name:"tool", arguments:args }（JSON-RPC）
 ├─ ⑨ [可选] notifications/progress → progress_callback → renderer.info("进度 N%")
 │           （按 10% 里程碑展示，total 未知则不展示）
 └─ 返回 CallToolResult{ content / structuredContent / isError }
 │
 ▼
结果回到 Agent
 ├─ ⑩ McpService：catalog.format_result(result, redactor()) → 脱敏 + MAX_TOOL_OUTPUT 截断
 ├─ ⑪ 结果字符串 → renderer.tool_result 上屏
 └─ ⑫ session.append({ role:"tool", content, tool_call_id })
 │
 ▼
模型  ◄── ⑬ 下一轮请求携带工具结果，继续推理
```

失败与中断分支（都翻译成给模型的文本，不抛异常）：

| 触发 | 位置 | 结果 |
| --- | --- | --- |
| 工具超时 | `SdkConnection._wait` 到 deadline | `future.cancel()` → `McpError("调用 X 超时")` → `错误: MCP srv.tool: …` |
| Esc 中断 | `_wait` 轮询到 `token.cancelled` | `future.cancel()` → `McpError("用户中断")` |
| 连接断开 | 泵任务 EOF → `_eof`；`_on_closed` 置 FAILED 并反注册 | `_wait` 抛"连接已断开"；状态变为失败 |
| server 报错 | `isError=true` | 结果加 `错误: ` 前缀，仍作为工具结果回传 |
| 权限拒绝 / 非交互 ask | `_preflight` → `_dispatch_ask` | 任务终止，剩余调用补 SKIPPED 占位 |

关键点：MCP 调用发生在 **Agent run 线程**（serial 屏障），loop 线程只承载协议与网络；
renderer 从 loop 线程直接调用（后端线程安全）；门面完成结果归一，`catalog` 与注册表无感知；
无论成功、失败还是中断，每个 `tool_call_id` 都恰有一条 `role=tool` 消息。

### 10.4 时序（连接）

```
Agent.start ─► McpService.start ─► ThreadPool ─► SdkConnection.start
                                      │              │
                                      │              ├─ AsyncRuntime.start (loop 线程)
                                      │              ├─ _open_transport (stdio/http/sse)
                                      │              ├─ Client.__aenter__ (握手)
                                      │              └─ list_tools
                                      ▼
                            回写前校验 generation / 身份
                                      │
                                      ├─ 成功：_sync_tools → register_dynamic → CONNECTED
                                      └─ 失败：FAILED / MISSING_ENV / NEEDS_AUTH
```

## 11. 命令与向导

### 11.1 `/mcp` 命令族（`commands/mcp.py`）

| 命令 | 行为 |
| --- | --- |
| `/mcp` | 弹选择面板：添加项 + 各服务器（名称 / 工具数 / 级别 / 状态着色）；选中服务器进操作菜单 |
| `/mcp list` | 文本状态列表（含 diagnostics） |
| `/mcp add` | TUI 居中向导 / REPL 行式；非交互下无参只给用法 |
| `/mcp add <名> -- <命令...> [-e K=V]` | stdio 直通：字面值存凭据库，配置只留 `${VAR}` |
| `/mcp add <名> --url <地址> [--type http\|sse] [--header K=V] [--oauth]` | 远程直通 |
| `/mcp tools\|logs\|reconnect\|enable\|disable\|remove\|auth <名>` | 工具列表 / 日志 / 重连 / 启停 / 删除 / OAuth 授权 |
| `/mcp <名>` | 服务器操作菜单（含 OAuth 服务器时多一项「OAuth 授权」） |

TUI 的 busy 守卫覆盖 `add|remove|enable|disable|reconnect|auth`——任务运行中改 MCP
配置会撕裂进行中的轮次，先行拦截。

### 11.2 添加向导（`wizard.py`）

纯状态机，不读写文件、不连接，TUI 面板与 REPL 行式流程共用；完成后 `apply_plan`
先存密钥再写配置并后台连接。分支：

```
添加方式
 ├─ 模板：内置模板（stdio：filesystem/github/playwright/memory/everything；
 │         远程：linear/sentry 带 url+oauth）
 ├─ 手动命令：启动命令 → 环境变量名 → 密钥存放方式（凭据库 / 引用环境变量 / 跳过）
 └─ 远程 URL：URL → 传输（http/sse）→ 鉴权（OAuth / 请求头）→ 请求头 K=V
                └─ 含 ${VAR} 的值原样引用；字面值自动存入凭据库
        ↓
      名称（模板/URL 推导默认）→ 作用域（全局/项目）→ 密钥步骤（stdio 有）→ 预览确认
```

预览页展示脱敏后的配置片段、写入位置与风险提示；远程 OAuth 分支提示保存后运行
`/mcp auth`。

## 12. 线程模型与并发

| 线程 | 用途 |
| --- | --- |
| 主 / Agent run 线程 | 命令、权限确认、serial 工具执行（含 MCP 调用） |
| `smithcode-mcp`（4 workers） | 后台连接、工具刷新（`_connect` / `_refresh_tools`） |
| `smithcode-mcp-loop` | 唯一的 asyncio 事件循环：SDK 协议、OAuth 流程、订阅、进度 |
| SDK 内部读/泵任务 | 由 loop 线程上的 anyio task group 管理；stdio 子进程的 piping 由 SDK 线程/子进程处理 |
| `smithcode-oauth-callback` | 交互授权时的本地 HTTP 回调 |

同步与锁：

- `McpService._lock`（RLock）：`_entries` / `_order` / state 的读写；
- `tools/base._LOCK`（RLock）：动态注册表增删；
- `SdkConnection._lock`：关闭状态；`AsyncRuntime._lock`：loop 生命周期；
- 渲染后端本身线程安全（ConsoleRenderer 打印；TuiRenderer `post_message`），
  因此 loop 线程的进度 / 回调可直接调用 renderer。

竞态要点见 §8.3（代际 + 身份）；OAuth token 文件在单进程内由 loop 串行读写。

## 13. 诊断与可观测性

- **状态**：`/mcp` / `/mcp list` 展示 state、工具数、作用域、错误与 diagnostics；
- **日志**：stdio server 的 stderr 落临时文件、传输异常记入备注，`/mcp logs <名>`
  取尾部并脱敏；
- **错误分层**：`McpConfigError`（配置）/ `McpAuthError`（授权）/ `McpError`（连接、
  协议、超时、取消），全部面向前用户的中文文案；
- **脱敏**：工具结果、stderr、日志、向导预览统一过 `Redactor`；OAuth token 与
  `${VAR}` 展开值都登记在案。

## 14. 测试策略

| 类型 | 位置 | 说明 |
| --- | --- | --- |
| 配置 / 密钥 / 目录 | `test_mcp_config.py` / `test_mcp_secrets.py` / `test_mcp_catalog.py` | 纯逻辑，隔离 `SMITHCODE_HOME` 与工作区 |
| 连接层（stdio） | `test_mcp_connection.py` | 真实假 server 子进程：握手、调用、超时、取消、崩溃、`list_changed`、进度节流 |
| 连接层（HTTP / SSE） | `test_mcp_connection.py` | 真实 SDK `MCPServer` + uvicorn：Streamable HTTP 往返 / 请求头 / 进度 / 现代订阅；`sse_app()` 起真实 SSE 会话 |
| 服务层 | `test_mcp_service.py` | 后台连接、动态注册、启停、删除、跨作用域冲突、项目作用域 |
| Agent 集成 | `test_agent_mcp.py` | 工具进入请求 schema、调用执行、默认权限拒绝、stop 反注册 |
| OAuth | `test_mcp_auth.py` | token 存储 / 脱敏 / 0600、非交互拒绝；最小授权服务器 + 受保护 server 的端到端授权与静默复用 |
| 命令 / 向导 / TUI | `test_commands_mcp.py` / `test_mcp_wizard.py` / `test_tui_mcp.py` | 解析、直通写盘、向导分支、面板 |
| 第三方真实 server（opt-in） | `test_mcp_connection.py::test_real_everything_server_smoke` | `SMITHCODE_MCP_E2E=1` 时对官方 `@modelcontextprotocol/server-everything` 冒烟（npx + 网络，默认跳过） |

## 15. 已知边界与后续扩展

- **协议能力**：仅 tools；resources / prompts / sampling / elicitation 未接入（SDK 已支持，
  回调骨架可直接接）。
- **SSE**：legacy 且 SDK 未作为推荐路径；已实现并集成测试，但不排除未来 server 弃用。
- **`reload()`**：已实现配置 diff 与重连，但尚无命令入口（预留 `/mcp reload`）。
- **OAuth**：token 文件按服务器名分键，未按资源 URL 多租户；CIMD（Client ID Metadata
  Documents）与 client_credentials（机器到机器）未接入。
- **并发**：单共享 loop 线程；单个 server 的事件循环阻塞会拖慢其他 server（已约定
  loop 内不阻塞）。
- **敏感值脱敏**：短于 4 字符的值不参与替换（取舍），Redactor 只增不减。

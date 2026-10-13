# SmithCode 桌面端

Tauri 2 + React + Vite + TS 薄客户端，后端是 `smith --serve` sidecar（见 `packages/core/src/smithcode/serve.py`）。
界面是 ChatGPT / Codex 风格的浅色布局（深色可切换）：左侧栏管项目与会话，中间是对话与输入区，右侧是文件 / 浏览器 / 终端与更改面板。

## 开发

```bash
# 终端 1：起 serve（Tauri 未接管时手动起；壳会自动拉起）
cd packages/core
SMITHCODE_HOME=~/.smithcode SMITH_AUTH_TOKEN=dev-token uv run python -m smithcode --serve --port 18789
# token 固定成 dev-token。dev（非 Tauri）下 BootScreen 会提示手填，填进 localStorage
# 的 smith.token 即可；Tauri 壳里 token 只走内存（见「鉴权」），不落 localStorage。
# 不设 SMITH_AUTH_TOKEN 则由 serve 随机生成、首次启动打印 token=…，同样手填。

# 终端 2：起 Web UI
cd packages/desktop
pnpm install
pnpm dev        # http://localhost:1420（无 Rust 也可联调 serve）
pnpm tauri dev  # 完整桌面壳（需 cargo + tauri-cli）
```

## 验证

```bash
pnpm typecheck   # tsc --noEmit
pnpm build       # vite build → dist/（tauri.conf.json frontendDist 即它）
cd src-tauri && cargo test   # 壳侧守卫：token 不进 argv、等待子进程不持锁、/health 探针
```

## 界面结构

- **左侧栏**：顶部是会话导航（←/→ 会话历史）、当前项目下拉（打开目录… / 复制路径 / 刷新 /
  设置）与搜索；下面是「新会话 / 插件（MCP 服务器）/ 探索（技能）」三个入口；再下面是
  **项目**（全部项目，点一行即切换：只改 serve 的默认工作区与历史过滤，**已开的 tab 一个
  不关**；行尾是会话数，下面挂「打开目录…」与当前项目最近的会话）与 **最近**（当前项目的
  会话，点开即恢复，悬停出 ⋯ 菜单：重命名 / 关闭 / 删除）；底部是账户卡（当前模型 + 版本 +
  连接状态）与设置。搜索（Ctrl+K）是跨项目的（`?all=1`），不受当前项目过滤影响。
- **主区**：空状态显示 Logo 与「在 {工作区} 里做点什么？」，有对话时是消息列；右上角是
  全屏与折叠侧栏。
- **右侧**：折叠态是快捷列（文件 `Ctrl+P` / 浏览器 `Ctrl+T` / 终端 `` Ctrl+` `` / 更改 `Ctrl+D`，
  「更改」只在当前会话有 diff 时出现）；点开后面板占位，文件面板只读浏览**当前会话**的工作区
  （路径经 `sessions/{id}/fs/*` 按会话沙箱解析，切项目/切会话自动重载）。
- **输入区**：文件夹 chip + 大圆角输入框；底部一行是 `+`（斜杠命令）、权限模式 pill、
  模型·强度、发送 / 停止按钮；排队消息以 chip 显示在输入框上方。

关闭会话 = `serve.drop`（从内存卸载），**日志保留**；⋯ 菜单里的删除 = `serve.purge`
（卸载 + 删磁盘日志，不可恢复，有二次确认）。

## 快捷键

菜单加速键由原生菜单（文件 / 编辑 / 视图 / 帮助）承担，前端只保留下列按键；
浏览器里跑 `pnpm dev` 时，菜单键由前端 JS 兜底绑定（`isTauri()` 判定，不会双触发）。

| 按键 | 动作 |
|---|---|
| `Cmd/Ctrl+N` | 新会话 |
| `Cmd/Ctrl+W` | 关闭当前会话 |
| `Cmd/Ctrl+K` | 搜索会话 |
| `Cmd/Ctrl+B` | 折叠 / 展开侧栏 |
| `Cmd/Ctrl+P` / `Ctrl+T` / `` Ctrl+` `` / `Ctrl+D` | 文件 / 浏览器 / 终端 / 更改面板 |
| `Cmd/Ctrl+,` | 设置 |
| `Ctrl+Tab` / `Ctrl+Shift+Tab` | 切换会话 |
| `Enter` / `Cmd+Enter` | 发送（输入框；命令补全打开时先补全） |
| `Shift+Enter` | 换行 |
| `Esc` | 中断任务 / 关闭弹窗（permission 按 deny、提问按 cancel 收尾） |
| `↑/↓` | 输入框空时翻历史；命令补全打开时选命令 |

## 依赖说明

- `react-markdown + remark-gfm`：正文 markdown（含表格/任务列表），流式按 fence 切分增量渲染。
- `rehype-highlight + highlight.js`：代码块高亮；配色在 `src/styles/tokens.css` 里按主题变量定义
  （浅/深各一套），不再引 highlight.js 自带的主题 CSS（那些是固定色的，换主题要动态换表）。
- `lucide-react`：图标。`zustand`：会话状态。
- 不引组件库 / 编辑器 / xterm：终端与浏览器面板是二期。

## 与 serve 的契约

- 类型：`import type ... from "../../protocol/events"`（生成物，只读引用，不手改）。
- 下行：`GET /events?session_id=&cursors=&limit=&ticket=`（SSE）。**不用原生 `EventSource`**：
  它设不了 `Authorization` header，只能把 token 拼进 URL（日志/历史/dump 都收）。改用
  `fetch + ReadableStream` 自读流，头带 `Authorization`。
  - **一次性票据**：建流前先 `POST /events/ticket`（header 鉴权）换一张 30s、**一次有效**
    的票据，再拼 `?ticket=`。长效 token 永不进 URL。
  - **游标按会话各记一份**（`cursors` = `{"<sid>": seq}` JSON）：跨会话共用一个游标时，
    打开旧会话（其 seq 通常小于游标）会把整段历史跳过去。服务端兼容旧的单 `after_seq`。
  - **重放有上限**（`limit`，默认 500）：超限即截断并发 `replay.truncated` 帧带续跑游标，
    前端据此推进游标、下次重连继续拉（避免大会话一次全量重放）。
  - durable 事件走两路：连接时按游标**重放**日志补历史，之后的**直播**（订阅者拿到的信封
    `seq` 是 None——seq 由日志写盘时才分配，见 `event/envelope.py`）。重复由前端按
    **事件 id** 去重（日志副本与直播副本 id 相同），更旧的按 `seq` 水位丢掉。
  - 服务端**不做**这套去重：它区分不出"这条是否已在重放里发过"（durable 的直播信封同样
    没有 seq），按 seq 猜会把 volatile（排队 chip、流式增量、通知、忙碌态）一起吞掉，
    表现就是"消息发出去后界面什么都不显示"。
- 断线：指数退避重连（backoff/timer 存 ref，卸载清干净）；volatile 不重放，弹窗状态用
  `GET pending` 补。未知会话（已 purge）返回 404，前端**不重连**。
- 运行中发送**不拦**：后端按 `[queue]` 配置排队（排队 chip 的取回/撤销走
  `POST /sessions/{id}/queue/take|cancel`），Composer 在运行中仍接受 Enter / 发送按钮
  （空输入时才显示停止按钮，Esc 也能中断）。
- 上行：`POST /sessions/{id}/prompt|prompt_async|interrupt|dispatch|permission`、
  `POST reply`、`GET pending`。`prompt` 同步等回执（服务端 600s 上限）；`prompt_async`
  202 直接回、不抱 HTTP 线程（长任务用）。
- 管理面：`GET|POST /workspace`（切项目）、`GET|POST /projects`（列表 / 登记目录）、
  `GET /commands`、`GET /fs/list|/fs/read` 与 `GET /sessions/{id}/fs/list|read`（按会话沙箱的
  只读浏览）、`GET /sessions/history`（`?project=` / `?all=`）、`POST /sessions/{id}/purge|rename`、
  `GET /sessions/{id}/workspace|skills`、`POST /auth/rotate`（轮换 token）、`/mcp/servers*`
  （列表 / 日志 / 工具 / 启停 / 重连 / 授权 / 删除 / 添加）。这些都封装在 `session/serve.ts` 里。
- 问询：`PromptStarted(id)` → 弹窗 → `POST reply` → `PromptFinished`（同 id 配对关闭）。
- 命令：`/技能名` 的载荷注入与续跑在 serve 侧完成（既不是前端拼 prompt，也不会重复开跑）；
  运行中执行这类命令会回 `skipped_busy`，前端提示等待或中断。

## 主题

- `src/app/useTheme.ts`：三档 `light / dark / system`，存 localStorage（`smith.theme`），
  写 `html[data-theme]`；`main.tsx` 在首帧前应用，避免亮暗闪烁。
- `src/styles/tokens.css`：全部颜色/圆角/阴影变量 + 代码高亮配色，浅色是默认值，
  `html[data-theme="dark"]` 覆盖。

## sidecar 托管（Rust 壳）

- 启动：挑空闲端口（优先 18789，被占则换）→ spawn（`smith` → 回退 `python3 -m smithcode`，`SMITH_BIN` 可覆盖）→ 20s **HTTP `/health` 探针**（要求 200 + `{"ok":true}`；纯 TCP 握手会把占住端口的任意进程当成自己的 serve）。**不复用**"已经在跑的 serve"：那需要把 token 落盘、再带着它去探身份，等于把密钥交给占住端口的任意本机进程，换来的只是一个几百毫秒的省事。
- 鉴权：token 只经 `SMITH_AUTH_TOKEN` 环境变量进子进程——**不进 argv**（`ps` / `/proc/<pid>/cmdline` 对同机所有用户可见），**不落盘**。首次启动由 serve 随机生成、从 stdout 的 `token=` 行回读；之后（崩溃重启）沿用内存里那份，前端不必重新取。前端经 `sidecar_token` 命令取，**只存在内存**（`session/serve.ts` 的 `memToken`），绝不写 localStorage——那里任何 JS 都能读。需要轮换时 `POST /auth/rotate`。
- 来源白名单：serve 只放行 `tauri://localhost`、`http(s)://tauri.localhost`（Windows）、`http://localhost:1420`（dev），其余 Origin 一律 403——绑回环拦不住网页（浏览器会向 `127.0.0.1` 发跨站请求，见 Claude Code IDE 扩展 CVE-2025-52882）。此外 serve 还核验 **Host 白名单**（只放行回环 host[:port]）：DNS-rebind（`evil.com` → `127.0.0.1`）的请求没有可信 Origin，但 Host 一定是攻击者的域名。端口/来源不合预期时用 `SMITH_ALLOWED_ORIGINS`（逗号分隔）追加；被拒的 Origin/Host 会打在 serve stdout（壳记进日志）。
- 保活：崩溃 2s 后重启，上限 5 次；**跑够 60s 视为稳定，预算重置**（长跑后偶发崩溃不会累积）；`sidecar_restart`（手动）也重置预算。窗口 `Destroyed` 时 kill 子进程，且 spawn 与入库之间做**关窗竞态双检**（否则那段窗口期会留下杀不掉的孤儿）。stdout/stderr 各一个线程抽干（不抽干管道写满会卡死子进程），日志记 `~/.local/state/smithcode/smithcode-sidecar.log`：`token=` 行脱敏、其余行**逐行 scrub**（serve 的 stderr 可能把 token/密钥原样打出来），超 2 MiB 轮转到 `.1`。
- 菜单：Rust 侧建原生菜单（文件 / 编辑 / 视图 / 帮助），命中后 `emit("smith://menu", id)`，
  前端 `app/desktop.ts` 的 `listenMenu` 分发到 App 的具体动作。
- CSP：`tauri.conf.json` 的 `app.security.csp`（prod，禁远程脚本）与 `devCsp`（dev，放行
  Vite HMR 的 `localhost:1420` + `unsafe-inline`）；`connect-src` 放行 `127.0.0.1:*`（serve）
  与 Tauri `ipc:`。capabilities 的 `shell:allow-spawn` 参数收窄到 `--serve --port <数字>`。
- 版本：`scripts/sync_versions.py` 以 core `__version__` 为准同步三处（`--check` 供 CI）。

## 打包

```bash
cd packages/desktop
./build_sidecar.sh   # pyinstaller 打 sidecar → src-tauri/binaries/smith-<triple>（gitignore，现打现用）
pnpm tauri build     # deb + rpm + AppImage（sidecar 自动打进 usr/bin/smith）
```

sidecar 在包内的最终位置是 `usr/bin/smith`（externalBin），壳优先用它，
找不到才回退 `PATH` 的 `smith` / `python3 -m smithcode`（dev 行为不变）。
`SMITH_BIN` 环境变量可强制覆盖二进制路径。

## 范围

已做：浅色/深色双主题、ChatGPT 风格布局（侧栏 项目/最近、空状态、右 rail、Composer）、
会话管理（新建 / 恢复 / 重命名 / 删除 / 搜索 / 导航历史）、**多项目工作区**（侧栏项目切换、
「打开目录…」登记并切换、历史按项目过滤、新建会话落在当前项目、恢复跨项目会话沙箱跟会话走、
文件面板按会话沙箱）、消息渲染（气泡 / 工具行 / 思考 / 计划 / 队列）、问答弹窗、文件面板、
变更面板、设置（外观 / 模型服务 / MCP / 技能 / 关于）、原生菜单与快捷键、工作区信息与文件
浏览接口。

二期：终端面板（PTY + xterm.js）、浏览器面板（内嵌 webview）、消息内联 diff、原生目录选择器
（Tauri dialog 插件，替代手输路径的「打开目录…」）、`--workspace` 启动参数与上次项目记忆、
项目级 MCP 与每项目技能白名单。
